"""
Feature 8 — NamespaceType (work vs reference).

Contract under test:

  * Every namespace has a type, defaulting to 'work' if never set.
  * Search **defaults to work only** — reference-typed namespaces are
    excluded unless the caller passes ``include_reference=true``.
  * GET  /admin/namespaces/{name}/type      → returns the type
  * PUT  /admin/namespaces/{name}/type?namespace_type=reference
                                            → flips it
  * GET  /admin/namespaces/types            → lists all with types
  * Setting a namespace to ``reference`` invalidates the cache and the
    very next search excludes it (no restart required).

Regression case — the cms57f → gridops leak we just hit in production:
  * Seed 'reference' namespace with a CMS-57F-style memory.
  * Seed 'work' namespace with an gridops-style memory that mentions FHIR.
  * Default search for an FHIR-related gridops query must NOT return the
    CMS-57F result.
  * ``include_reference=true`` MUST return both.
"""
from __future__ import annotations

import time
import uuid

import pytest


def _new_ns(label: str) -> str:
    return f"f8-{label}-{int(time.time())}-{uuid.uuid4().hex[:8]}"


def _write(http, ns: str, content: str, memory_type: str = "fact") -> dict:
    r = http.post(
        "/api/v1/memory/",
        json={"content": content, "namespace": ns, "memory_type": memory_type},
    )
    assert r.status_code == 201, f"{r.status_code} {r.text}"
    return r.json()


def _set_type(http, ns: str, t: str) -> dict:
    r = http.put(f"/api/v1/admin/namespaces/{ns}/type", params={"namespace_type": t})
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    return r.json()


def _get_type(http, ns: str) -> str:
    r = http.get(f"/api/v1/admin/namespaces/{ns}/type")
    assert r.status_code == 200
    return r.json()["namespace_type"]


# ---------------------------------------------------------------------------
# Defaults + admin endpoints
# ---------------------------------------------------------------------------

class TestDefaultsAndAdmin:
    def test_unset_namespace_defaults_to_work(self, http):
        ns = _new_ns("default")
        # Don't write anything — type is queryable without any memories
        assert _get_type(http, ns) == "work"

    def test_set_namespace_to_reference_persists(self, http):
        ns = _new_ns("flip")
        _write(http, ns, "seed memory in flip namespace")
        _set_type(http, ns, "reference")
        assert _get_type(http, ns) == "reference"

    def test_invalid_type_rejected(self, http):
        ns = _new_ns("invalid")
        r = http.put(
            f"/api/v1/admin/namespaces/{ns}/type",
            params={"namespace_type": "garbage"},
        )
        assert r.status_code == 422

    def test_list_namespace_types_returns_both(self, http):
        work_ns = _new_ns("listwork")
        ref_ns = _new_ns("listref")
        _write(http, work_ns, "operational memory")
        _write(http, ref_ns, "reference memory")
        _set_type(http, ref_ns, "reference")

        listed = http.get("/api/v1/admin/namespaces/types").json()
        by_name = {item["name"]: item["namespace_type"] for item in listed}
        assert by_name.get(work_ns) == "work"
        assert by_name.get(ref_ns) == "reference"


# ---------------------------------------------------------------------------
# Search defaults — reference excluded
# ---------------------------------------------------------------------------

class TestSearchDefaults:
    def test_default_search_excludes_reference_namespace(self, http):
        ref_ns = _new_ns("ref-default")
        ref_mem = _write(http, ref_ns, "reference doc about widget compliance policy")
        _set_type(http, ref_ns, "reference")

        # Search across all namespaces — reference must be filtered out
        results = http.get(
            "/api/v1/memory/search",
            params={"q": "widget compliance policy", "ns": "all", "mode": "vector", "top_k": 10},
        ).json()
        ids = {r["id"] for r in results}
        assert ref_mem["id"] not in ids, (
            "reference-typed memory leaked into default search"
        )

    def test_include_reference_returns_reference_results(self, http):
        ref_ns = _new_ns("ref-optin")
        ref_mem = _write(http, ref_ns, "reference doc about widget compliance policy")
        _set_type(http, ref_ns, "reference")

        results = http.get(
            "/api/v1/memory/search",
            params={
                "q": "widget compliance policy", "ns": "all",
                "mode": "vector", "top_k": 10, "include_reference": "true",
            },
        ).json()
        ids = {r["id"] for r in results}
        assert ref_mem["id"] in ids, "reference memory should appear when opted in"


# ---------------------------------------------------------------------------
# Regression — exact cms57f → gridops leak we hit in production
# ---------------------------------------------------------------------------

class TestCmsgridopsLeakRegression:
    """The exact scenario that broke developer trust:
       a CMS-0057-F reference doc bled into an gridops engineering query."""

    def test_cms57f_does_not_leak_into_gridops(self, http):
        gridops_ns = _new_ns("gridops")
        cms_ns = _new_ns("cms57f")

        # gridops (work) — engineering memory mentioning FHIR
        gridops_mem = _write(
            http, gridops_ns,
            "PROJ-445 fix: ClaimResponse FHIR profile citation reverted; "
            "MR posted by Sai; awaiting QA verification",
            memory_type="fact",
        )

        # cms57f (reference) — policy doc that vector-matches "FHIR"
        cms_mem = _write(
            http, cms_ns,
            "CMS-0057-F mandates payer-to-payer FHIR Bulk Data Access at "
            "$.95 percentile within 1 business day for active members",
            memory_type="fact",
        )
        _set_type(http, cms_ns, "reference")

        # Developer's query — pure gridops context, no expectation of policy text
        results = http.get(
            "/api/v1/memory/search",
            params={"q": "PROJ-445 FHIR ClaimResponse fix", "ns": "all",
                    "mode": "rrf", "top_k": 5},
        ).json()
        ids = {r["id"] for r in results}

        # gridops memory is what they wanted
        assert gridops_mem["id"] in ids, (
            f"gridops memory missing from results: {results}"
        )
        # cms57f memory MUST NOT leak — this was the credibility-breaking bug
        assert cms_mem["id"] not in ids, (
            f"REGRESSION: cms57f reference memory leaked into gridops query. "
            f"results={[r['content'][:60] for r in results]}"
        )

    def test_opting_in_surfaces_cms57f_for_grounding(self, http):
        """When the user EXPLICITLY wants policy context, they can ask for it.

        Uses a unique token in both seeds so the query is isolated from
        prior runs — protects double-pass safety even when many other
        reference-typed memories already exist in the dev stack.
        """
        tag = f"optin-{uuid.uuid4().hex[:8]}"
        gridops_ns = _new_ns("gridops-opt")
        cms_ns = _new_ns("cms57f-opt")
        gridops_mem = _write(http, gridops_ns, f"PROJ-445 {tag} fix ClaimResponse FHIR profile")
        cms_mem = _write(http, cms_ns, f"CMS-0057-F {tag} payer-to-payer FHIR requirement")
        _set_type(http, cms_ns, "reference")

        results = http.get(
            "/api/v1/memory/search",
            params={
                "q": tag, "ns": "all",
                "mode": "rrf", "top_k": 10, "include_reference": "true",
            },
        ).json()
        ids = {r["id"] for r in results}
        assert gridops_mem["id"] in ids, f"gridops seed missing; results={ids}"
        assert cms_mem["id"] in ids, f"opt-in must surface reference content; results={ids}"


# ---------------------------------------------------------------------------
# Cache invalidation — set-type takes effect immediately
# ---------------------------------------------------------------------------

class TestCacheInvalidation:
    def test_flipping_to_reference_takes_effect_on_next_search(self, http):
        ns = _new_ns("cache")
        mem = _write(http, ns, "memory to be hidden after retype")

        # Before retype — search finds it
        before = http.get(
            "/api/v1/memory/search",
            params={"q": "memory to be hidden after retype", "ns": "all",
                    "mode": "vector", "top_k": 5},
        ).json()
        assert any(r["id"] == mem["id"] for r in before)

        # Flip namespace to reference
        _set_type(http, ns, "reference")

        # After retype — same query no longer surfaces the memory by default
        after = http.get(
            "/api/v1/memory/search",
            params={"q": "memory to be hidden after retype", "ns": "all",
                    "mode": "vector", "top_k": 5},
        ).json()
        assert all(r["id"] != mem["id"] for r in after), (
            "cache was not invalidated; flipped namespace still returns in default search"
        )
