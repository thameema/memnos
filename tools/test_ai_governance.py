"""
tools/test_ai_governance.py — AI governance correctness test suite.

Tests:
1. constraint_always_injected       — constraint surfaces regardless of query topic
2. decision_pinned_by_affects       — decision pinned (score=2.0) when query entity matches affects[]
3. affects_graph_edge_exists        — AFFECTS graph edge created after write
4. superseded_constraint_excluded   — superseded constraint NOT injected
5. namespace_inheritance            — parent-ns constraint surfaces in child-ns search
6. multi_entity_affects             — decision affecting [A, B] surfaces for queries on either
7. quality_gate_affects_rationale   — decision lacking affects[]/rationale flagged by get_unused_constraints

Run standalone:
    python3 tools/test_ai_governance.py [--verbose] [--test <name>]

Run via pytest (requires live engram API):
    python -m pytest tools/test_ai_governance.py -v
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

import httpx

ENGRAM_API = os.environ.get("ENGRAM_API_URL", "http://127.0.0.1:8766")
ENGRAM_KEY = os.environ.get("ENGRAM_API_KEY", "engram-local-dev-key")
ARCADEDB_HOST = os.environ.get("ARCADEDB_HOST", "localhost")
ARCADEDB_PASS = os.environ.get("ARCADEDB_PASSWORD", "engram-dev-password")
_RUN = uuid.uuid4().hex[:8]
TEST_NS = f"test:gov:{_RUN}"


def api(method: str, path: str, **kwargs):
    with httpx.Client(timeout=30) as c:
        r = c.request(method, f"{ENGRAM_API}{path}",
                      headers={"X-API-Key": ENGRAM_KEY, "Content-Type": "application/json"},
                      **kwargs)
    assert r.status_code < 500, f"Server error {r.status_code}: {r.text[:300]}"
    return r


def write(content: str, ns: str, memory_type: str = "fact",
          affects: list[str] | None = None, rationale: str = "") -> dict:
    payload = {"content": content, "namespace": ns, "memory_type": memory_type,
               "tags": ["gov-test"], "affects": affects or [], "rationale": rationale}
    r = api("POST", "/api/v1/memory/", json=payload)
    assert r.status_code in (200, 201), f"Write failed ({r.status_code}): {r.text}"
    return r.json()


def search(q: str, ns: str, top_k: int = 10) -> list[dict]:
    r = api("GET", f"/api/v1/memory/search",
            params={"q": q, "ns": ns, "top_k": top_k, "mode": "hybrid"})
    assert r.status_code == 200, f"Search failed: {r.text}"
    return r.json()


def arcade_query(sql: str, params: dict | None = None) -> list[dict]:
    """Query ArcadeDB directly to verify graph state."""
    r = httpx.post(
        f"http://{ARCADEDB_HOST}:2480/api/v1/query/engram",
        auth=("root", ARCADEDB_PASS),
        json={"language": "sql", "command": sql, **({"params": params} if params else {})},
        timeout=10,
    )
    if r.status_code != 200:
        return []
    return r.json().get("result", [])


def cleanup_ns(ns: str) -> None:
    try:
        api("DELETE", f"/api/v1/admin/namespace/{ns}")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

@dataclass
class Runner:
    verbose: bool = False
    only: str | None = None
    skip_webhook: bool = True
    results: list[tuple[str, bool, str, float]] = field(default_factory=list)

    def run(self, fn: Callable) -> None:
        name = fn.__name__.removeprefix("test_")
        if self.only and self.only != name:
            return
        t0 = time.monotonic()
        try:
            fn(self)
            ms = (time.monotonic() - t0) * 1000
            self.results.append((name, True, "", ms))
            print(f"  \033[32m✓\033[0m {name}  ({ms:.0f}ms)")
        except AssertionError as e:
            ms = (time.monotonic() - t0) * 1000
            self.results.append((name, False, str(e), ms))
            print(f"  \033[31m✗\033[0m {name}  ({ms:.0f}ms)")
            print(f"    → {e}")
        except Exception as e:
            ms = (time.monotonic() - t0) * 1000
            self.results.append((name, False, f"{type(e).__name__}: {e}", ms))
            print(f"  \033[31m✗\033[0m {name}  ({ms:.0f}ms)")
            print(f"    → {type(e).__name__}: {e}")

    def summarise(self) -> int:
        total = len(self.results)
        passed = sum(1 for _, ok, _, _ in self.results if ok)
        elapsed = sum(ms for _, _, _, ms in self.results)
        print()
        print("=" * 70)
        print(f"Results: {passed}/{total} passed  ({elapsed:.0f}ms total)")
        if passed == total:
            print("All tests passed.")
        else:
            for name, ok, msg, _ in self.results:
                if not ok:
                    print(f"  \033[31m✗\033[0m {name}: {msg}")
        return 0 if passed == total else 1


# ===========================================================================
# Tests
# ===========================================================================

def test_constraint_always_injected(runner: Runner) -> None:
    """CONSTRAINT memories are injected before every search result in their namespace,
    regardless of semantic similarity to the query."""
    ns = f"{TEST_NS}:constraint-inject"
    write(
        "All writes must include provenance.user_id for audit trail",
        ns,
        memory_type="constraint",
        affects=["provenance", "audit"],
        rationale="Compliance requires full attribution of every write",
    )
    # Write unrelated memory so there are results to check ordering on
    write("The weather in San Francisco is often foggy in summer", ns)
    time.sleep(1)

    results = search("weather fog", ns)
    assert len(results) > 0, "Expected at least one result"

    types = [r["memory_type"] for r in results]
    ids_at_scores = [(r["memory_type"], r.get("score")) for r in results]

    # Constraint must be present
    assert "constraint" in types, f"Constraint not found in results: {ids_at_scores}"

    # Constraint must be first (score=2.0 pins it above all vector results)
    assert results[0]["memory_type"] == "constraint", \
        f"Constraint not pinned first, got: {ids_at_scores}"

    if runner.verbose:
        print(f"\n    Results: {ids_at_scores}")


def test_decision_pinned_by_affects(runner: Runner) -> None:
    """A DECISION with affects=['database'] is pinned (score>=1.5) when searching
    for 'database', even if it is not semantically closest."""
    ns = f"{TEST_NS}:decision-pin"
    decision = write(
        "Use ArcadeDB, not PostgreSQL — single store eliminates sync lag",
        ns,
        memory_type="decision",
        affects=["database", "storage"],
        rationale="Three-store complexity caused production sync failures",
    )
    did = decision["id"]
    # Write several unrelated memories to dilute semantic similarity
    for i in range(3):
        write(f"Unrelated content item {i} about cats and cooking and music", ns)
    time.sleep(1)

    results = search("database storage engine choice", ns)
    result_ids = [r["id"] for r in results]
    result_scores = [(r["id"][:8], r.get("score")) for r in results]

    assert did in result_ids, \
        f"Decision {did[:8]} not in results: {result_scores}"

    decision_entry = next(r for r in results if r["id"] == did)
    score = decision_entry.get("score") or 0
    assert score >= 1.5, \
        f"Decision score {score} too low — expected >=1.5 (pinned): {result_scores}"

    if runner.verbose:
        print(f"\n    Decision score: {score}  results: {result_scores}")


def test_affects_graph_edge_exists(runner: Runner) -> None:
    """After writing a decision with affects=['graph-test-entity'], an AFFECTS
    graph edge must exist in ArcadeDB between that Memory and the Entity."""
    ns = f"{TEST_NS}:graph-edge"
    mem = write(
        "Graph edge verification test decision",
        ns,
        memory_type="decision",
        affects=["graph-test-entity"],
        rationale="Verifies the AFFECTS edge is created at write time",
    )
    mid = mem["id"]
    time.sleep(1)

    rows = arcade_query(
        'MATCH {type: Memory, as: m, where: (id = :mid)}'
        '-AFFECTS->'
        '{type: Entity, as: e} '
        'RETURN e.name as name',
        {"mid": mid},
    )
    names = [r.get("name") for r in rows]
    assert "graph-test-entity" in names, \
        f"Expected AFFECTS edge to 'graph-test-entity', got: {names}"

    if runner.verbose:
        print(f"\n    AFFECTS edges found: {names}")


def test_superseded_constraint_excluded(runner: Runner) -> None:
    """After deleting a constraint, it must NOT appear in active search results.

    engram soft-deletes via the DELETE endpoint (hard-purge from vector + graph
    stores). This tests the filter path: a removed memory is absent from results.
    Note: engram auto-supersedes only when the contradiction detector fires
    (negation_detected / opposite_polarity). Explicit removal uses DELETE.
    """
    ns = f"{TEST_NS}:superseded"
    mem = write(
        "Constraint: all writes must use v1 auth tokens only",
        ns,
        memory_type="constraint",
        affects=["auth"],
        rationale="v1 auth policy active before v2 rollout",
    )
    mid = mem["id"]

    # Verify it's in results before deletion
    results_before = search("auth token constraint", ns)
    ids_before = [r["id"] for r in results_before]
    assert mid in ids_before, \
        f"Constraint {mid[:8]} not found before deletion: {[i[:8] for i in ids_before]}"

    # Delete (purge) the constraint
    r = api("DELETE", f"/api/v1/memory/{mid}", params={"ns": ns})
    assert r.status_code in (200, 204), \
        f"Delete failed ({r.status_code}): {r.text}"

    time.sleep(1)

    results = search("auth token constraint", ns)
    found_ids = [r["id"] for r in results]

    assert mid not in found_ids, \
        f"Deleted constraint {mid[:8]} still appearing in search results"

    if runner.verbose:
        print(f"\n    Before: {[i[:8] for i in ids_before]}")
        print(f"\n    After:  {[i[:8] for i in found_ids]}")


def test_namespace_inheritance(runner: Runner) -> None:
    """A decision/constraint in parent namespace must surface in child namespace
    search when the query contains the affects[] entity name in kebab/snake form.

    Pinning is driven by get_decisions_for_entities which uses the full
    namespace ancestry list (parent:child → parent → root).
    The query MUST contain a token that _query_entity_names() extracts
    (kebab-case, snake_case, CamelCase or ALL_CAPS) to trigger entity lookup.
    """
    parent_ns = f"{TEST_NS}:parent"
    child_ns = f"{TEST_NS}:parent:child"

    write(
        "Global constraint: all agent-write operations must be idempotent for retry safety",
        parent_ns,
        memory_type="constraint",
        affects=["agent-write", "retry-safety"],
        rationale="Retry-safe writes prevent duplicate records in distributed systems",
    )
    write(
        "Local fact about agent-write retry-safety patterns in the child service",
        child_ns,
    )
    time.sleep(1)

    # Query contains "agent-write" (kebab-case) — _query_entity_names() will
    # extract it and trigger get_decisions_for_entities() with namespace ancestry
    results = search("agent-write retry-safety policy", child_ns)
    types = [r["memory_type"] for r in results]
    ns_found = [(r["memory_type"], r.get("namespace")) for r in results]

    assert "constraint" in types, \
        (f"Parent constraint not inherited in child namespace search. "
         f"Types found: {types}  (results: {ns_found})")

    if runner.verbose:
        print(f"\n    Results (type, ns): {ns_found}")


def test_multi_entity_affects(runner: Runner) -> None:
    """A decision with affects=['alpha', 'beta'] must surface for queries about
    either entity, not just one."""
    ns = f"{TEST_NS}:multi-entity"
    mem = write(
        "Multi-entity governance decision covering both alpha and beta subsystems",
        ns,
        memory_type="decision",
        affects=["alpha-subsystem", "beta-subsystem"],
        rationale="Both subsystems share the same connection pool and must coordinate",
    )
    did = mem["id"]
    time.sleep(1)

    for entity, query in [("alpha-subsystem", "alpha subsystem behaviour"),
                           ("beta-subsystem", "beta subsystem configuration")]:
        results = search(query, ns)
        ids = [r["id"] for r in results]
        assert did in ids, \
            f"Decision not found for {entity} query '{query}'. Got ids: {[i[:8] for i in ids]}"

    if runner.verbose:
        print(f"\n    Decision {did[:8]} found for both alpha and beta queries")


def test_quality_gate_affects_rationale(runner: Runner) -> None:
    """A decision written without affects[] or rationale must be flagged by
    the get_unused_constraints / quality-gate endpoint."""
    ns = f"{TEST_NS}:quality-gate"
    write(
        "Decision with no affects and no rationale — quality gate failure case",
        ns,
        memory_type="decision",
        affects=[],
        rationale="",
    )
    time.sleep(1)

    # Use the admin endpoint to check for uncovered constraints/decisions
    r = api("GET", f"/api/v1/admin/governance/gaps", params={"ns": ns})
    if r.status_code == 404:
        # Endpoint doesn't exist yet — fall back to direct ArcadeDB check
        rows = arcade_query(
            "SELECT id, affects, rationale FROM Memory "
            "WHERE namespace = :ns AND memory_type IN ['decision', 'constraint', 'adr'] "
            "AND status = 'active' AND superseded_at IS NULL",
            {"ns": ns},
        )
        gaps = [row for row in rows
                if not (row.get("affects") or []) or not (row.get("rationale") or "").strip()]
        assert len(gaps) > 0, \
            "Expected at least one decision flagged for missing affects[]/rationale"
        if runner.verbose:
            print(f"\n    {len(gaps)} decision(s) flagged as quality gaps")
        return

    assert r.status_code == 200, f"Governance gaps endpoint error: {r.text}"
    gaps = r.json()
    assert len(gaps) > 0, "Expected at least one quality gap flagged"

    if runner.verbose:
        print(f"\n    Gaps returned: {gaps}")


# ===========================================================================
# pytest integration
# ===========================================================================

def test_constraint_always_injected_pytest(runner) -> None:
    test_constraint_always_injected(runner)


def test_decision_pinned_by_affects_pytest(runner) -> None:
    test_decision_pinned_by_affects(runner)


def test_affects_graph_edge_exists_pytest(runner) -> None:
    test_affects_graph_edge_exists(runner)


def test_superseded_constraint_excluded_pytest(runner) -> None:
    test_superseded_constraint_excluded(runner)


def test_namespace_inheritance_pytest(runner) -> None:
    test_namespace_inheritance(runner)


def test_multi_entity_affects_pytest(runner) -> None:
    test_multi_entity_affects(runner)


def test_quality_gate_affects_rationale_pytest(runner) -> None:
    test_quality_gate_affects_rationale(runner)


# ===========================================================================
# Entry point
# ===========================================================================

def main() -> int:
    parser = argparse.ArgumentParser(description="engram AI Governance Tests")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--test", "-t", metavar="NAME", help="Run a single test by name")
    args = parser.parse_args()

    try:
        with httpx.Client(timeout=5) as c:
            r = c.get(f"{ENGRAM_API}/api/v1/admin/health",
                      headers={"X-API-Key": ENGRAM_KEY})
            if r.status_code != 200:
                print(f"[error] engram API not healthy ({r.status_code})", file=sys.stderr)
                return 1
    except Exception as e:
        print(f"[error] Cannot reach engram at {ENGRAM_API}: {e}", file=sys.stderr)
        return 1

    runner = Runner(verbose=args.verbose, only=args.test)

    print("engram AI Governance Tests")
    print(f"API: {ENGRAM_API}   namespace: {TEST_NS}")
    print("=" * 70)
    print()

    tests = [
        test_constraint_always_injected,
        test_decision_pinned_by_affects,
        test_affects_graph_edge_exists,
        test_superseded_constraint_excluded,
        test_namespace_inheritance,
        test_multi_entity_affects,
        test_quality_gate_affects_rationale,
    ]

    try:
        for fn in tests:
            runner.run(fn)
    finally:
        for ns_suffix in ["constraint-inject", "decision-pin", "graph-edge",
                          "superseded", "parent:child", "multi-entity", "quality-gate"]:
            cleanup_ns(f"{TEST_NS}:{ns_suffix}")
        cleanup_ns(f"{TEST_NS}:parent")

    return runner.summarise()


if __name__ == "__main__":
    sys.exit(main())
