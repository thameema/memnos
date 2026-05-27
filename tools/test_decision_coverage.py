#!/usr/bin/env python3
"""
test_decision_coverage.py — Validate that projects have decisions and constraints
stored as graph vertices in engram.

This test answers: "Does each major project have its architectural decisions and
constraints captured in the right memory types, with enough quality to be useful
for automated code reviews?"

Value for code reviews
----------------------
When Claude reviews code in `payment-service`, it traverses AFFECTS edges to find
all Memory vertices where affects[] contains the service name and
memory_type IN ['decision', 'constraint', 'adr']. This surfaces "what rules apply
here" without the reviewer needing to know where to look.

These tests ensure that pipeline stays healthy:
  - Project namespaces have minimum decision/constraint coverage
  - affects[] is populated (graph traversal works)
  - rationale is non-empty (the WHY is captured, not just the WHAT)
  - Graph traversal from a component name returns governing constraints
  - decision/constraint/adr types are properly distinguished from facts

Usage:
    python3 tools/test_decision_coverage.py
    python3 tools/test_decision_coverage.py --verbose
    python3 tools/test_decision_coverage.py --test project_namespaces_have_decisions
    python3 tools/test_decision_coverage.py --namespace org:engram
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

try:
    import httpx
except ImportError:
    print("[error] Missing package: httpx  (pip install httpx)", file=sys.stderr)
    sys.exit(1)

ARCADEDB_URL = os.environ.get("ARCADEDB_URL", "http://localhost:2480")
ENGRAM_API   = os.environ.get("ENGRAM_API",   "http://localhost:8766")
ENGRAM_KEY   = os.environ.get("ENGRAM_KEY",   "engram-local-dev-key")
DB_NAME      = "engram"

# Namespaces and minimum required decision+constraint counts.
# Expand this dict as projects add their ADRs.
#
# min_quality_pct: fraction of decisions that must have both affects[] and
# rationale populated. Ratchet this up as you backfill existing memories.
# Set to 0.0 during bootstrapping; raise to 1.0 once all decisions are enriched.
PROJECT_NAMESPACES: dict[str, dict] = {
    "org:engram": {
        "min_decisions":   3,   # ArcadeDB choice, hook pipeline, namespace routing, etc.
        "min_constraints": 0,   # constraints not yet written for engram itself
        "key_components":  ["arcadedb", "hooks", "vault", "mcp"],
        "min_quality_pct": 0.75,  # ratchet to 1.0 after backfilling affects/rationale
    },
    "org:hc:engineering": {
        "min_decisions":   1,
        "min_constraints": 0,
        "key_components":  [],
        "min_quality_pct": 0.70,  # ratchet to 1.0 after backfilling affects/rationale
    },
}

# ---------------------------------------------------------------------------
# ArcadeDB helpers (same pattern as test_arcadedb.py)
# ---------------------------------------------------------------------------

def _auth() -> dict:
    pw = os.environ.get("ARCADEDB_PASSWORD", "engram-dev-password")
    creds = base64.b64encode(f"root:{pw}".encode()).decode()
    return {"Authorization": f"Basic {creds}", "Content-Type": "application/json"}


def arcade_query(sql: str, params: dict | None = None) -> list[dict]:
    body: dict[str, Any] = {"language": "sql", "command": sql}
    if params:
        body["params"] = params
    resp = httpx.post(
        f"{ARCADEDB_URL}/api/v1/query/{DB_NAME}",
        content=json.dumps(body), headers=_auth(), timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json().get("result", [])


def arcade_command(sql: str, params: dict | None = None) -> list[dict]:
    body: dict[str, Any] = {"language": "sql", "command": sql}
    if params:
        body["params"] = params
    resp = httpx.post(
        f"{ARCADEDB_URL}/api/v1/command/{DB_NAME}",
        content=json.dumps(body), headers=_auth(), timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json().get("result", [])


def engram_post(path: str, body: dict) -> dict:
    resp = httpx.post(
        f"{ENGRAM_API}{path}",
        content=json.dumps(body),
        headers={"Content-Type": "application/json", "X-API-Key": ENGRAM_KEY},
        timeout=15.0,
    )
    resp.raise_for_status()
    return resp.json()


def uid() -> str:
    return str(uuid.uuid4())


def now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------

@dataclass
class Result:
    name: str
    passed: bool
    elapsed_ms: float
    message: str = ""


@dataclass
class Runner:
    verbose: bool = False
    results: list[Result] = field(default_factory=list)

    def run(self, name: str, fn: Callable) -> None:
        start = time.perf_counter()
        try:
            fn(self)
            elapsed = (time.perf_counter() - start) * 1000
            self.results.append(Result(name, True, elapsed))
            status = f"  \033[32m✓\033[0m {name}  ({elapsed:.0f}ms)"
            print(status)
        except AssertionError as exc:
            elapsed = (time.perf_counter() - start) * 1000
            self.results.append(Result(name, False, elapsed, str(exc)))
            print(f"  \033[31m✗\033[0m {name}  ({elapsed:.0f}ms)")
            print(f"    → {exc}")
        except Exception as exc:
            elapsed = (time.perf_counter() - start) * 1000
            self.results.append(Result(name, False, elapsed, str(exc)))
            print(f"  \033[31m✗\033[0m {name}  ({elapsed:.0f}ms)")
            print(f"    → {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Helper: fetch all decision/constraint/adr memories for a namespace
# ---------------------------------------------------------------------------

def get_governance_memories(namespace: str) -> list[dict]:
    """Return all active decision, constraint, and adr memories for a namespace."""
    return arcade_query(
        "SELECT id, memory_type, content, affects, rationale, tags, created_at "
        "FROM Memory "
        "WHERE memory_type IN ['decision', 'constraint', 'adr'] "
        "  AND status = 'active' "
        "  AND superseded_at IS NULL "
        "  AND (namespace = :ns OR namespace LIKE :prefix) "
        "ORDER BY created_at DESC",
        {"ns": namespace, "prefix": f"{namespace}:%"},
    )


def get_facts_for_namespace(namespace: str) -> list[dict]:
    return arcade_query(
        "SELECT id, memory_type FROM Memory WHERE memory_type = 'fact' "
        "AND status = 'active' AND superseded_at IS NULL "
        "AND (namespace = :ns OR namespace LIKE :prefix)",
        {"ns": namespace, "prefix": f"{namespace}:%"},
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_governance_types_are_distinct_from_facts(runner: Runner) -> None:
    """decision, constraint, adr memories are stored as distinct types — not as facts.

    Inserts one of each governance type plus a fact, then verifies:
    - governance query returns only the three governance entries
    - fact query excludes governance entries
    - each governance entry retains its exact memory_type
    """
    ns = "test:coverage:type-check:" + uid()[:8]
    ids = {
        "decision":   uid(),
        "constraint": uid(),
        "adr":        uid(),
        "fact":       uid(),
    }

    for mtype, mid in ids.items():
        affects = ["auth-service"] if mtype in ("decision", "constraint", "adr") else []
        rationale = f"rationale for {mtype}" if mtype != "fact" else ""
        arcade_command(
            "INSERT INTO Memory SET "
            "id = :id, content = :content, namespace = :ns, "
            "created_at = :ts, superseded_at = null, "
            "tags = [:mtype], source = 'test', metadata = {}, "
            "memory_type = :mtype, status = 'active', "
            "author = 'test', affects = :affects, rationale = :rationale, "
            "expires_at = null, review_by = null, "
            "provenance = {}, content_embedding = []",
            {
                "id": mid, "content": f"Test {mtype} entry",
                "ns": ns, "ts": now_str(),
                "mtype": mtype, "affects": affects, "rationale": rationale,
            },
        )

    gov = get_governance_memories(ns)
    gov_ids   = {r["id"] for r in gov}
    gov_types = {r["id"]: r["memory_type"] for r in gov}

    # governance query must include all three governance entries
    for mtype in ("decision", "constraint", "adr"):
        assert ids[mtype] in gov_ids, \
            f"memory_type='{mtype}' not returned by governance query"

    # governance query must NOT include the fact
    assert ids["fact"] not in gov_ids, \
        "memory_type='fact' incorrectly returned by governance query"

    # each entry retains its exact type
    for mtype in ("decision", "constraint", "adr"):
        assert gov_types[ids[mtype]] == mtype, \
            f"Expected memory_type='{mtype}', got '{gov_types[ids[mtype]]}'"

    # fact query must exclude governance types
    facts = get_facts_for_namespace(ns)
    fact_ids = {r["id"] for r in facts}
    for mtype in ("decision", "constraint", "adr"):
        assert ids[mtype] not in fact_ids, \
            f"memory_type='{mtype}' incorrectly returned by fact query"

    # cleanup
    for mid in ids.values():
        arcade_command(
            "DELETE VERTEX FROM Memory WHERE id = :id AND namespace = :ns",
            {"id": mid, "ns": ns},
        )


def test_decision_requires_affects_and_rationale(runner: Runner) -> None:
    """A decision with populated affects[] and rationale is discoverable by component name.

    This is the core requirement for code review traversal: given a component name,
    find all decisions that govern it.
    """
    ns = "test:coverage:affects:" + uid()[:8]
    dec_id = uid()

    arcade_command(
        "INSERT INTO Memory SET "
        "id = :id, content = :content, namespace = :ns, "
        "created_at = :ts, superseded_at = null, "
        "tags = ['decision', 'adr'], source = 'test', metadata = {}, "
        "memory_type = 'decision', status = 'active', "
        "author = 'architect', "
        "affects = ['payment-service', 'order-service'], "
        "rationale = 'PCI-DSS requires event-driven writes, not synchronous DB access', "
        "expires_at = null, review_by = null, "
        "provenance = {}, content_embedding = []",
        {
            "id": dec_id,
            "content": "payment-service must never write to DB directly — use the event queue",
            "ns": ns, "ts": now_str(),
        },
    )

    # Traverse: find decisions governing payment-service
    rows = arcade_query(
        "SELECT id, affects, rationale FROM Memory "
        "WHERE memory_type IN ['decision', 'constraint', 'adr'] "
        "  AND status = 'active' AND superseded_at IS NULL "
        "  AND namespace = :ns",
        {"ns": ns},
    )

    matching = [
        r for r in rows
        if "payment-service" in [a.lower().strip() for a in (r.get("affects") or [])]
    ]

    assert len(matching) == 1, \
        f"Expected 1 decision for payment-service, got {len(matching)}"
    assert matching[0]["id"] == dec_id
    assert matching[0]["rationale"], \
        "rationale must be non-empty for code review usefulness"

    # Verify affects field is a list (not a scalar or null)
    assert isinstance(matching[0].get("affects"), list), \
        "affects must be a list to support multi-component governance"

    # Cleanup
    arcade_command(
        "DELETE VERTEX FROM Memory WHERE id = :id AND namespace = :ns",
        {"id": dec_id, "ns": ns},
    )


def test_constraint_is_injectable_before_search_results(runner: Runner) -> None:
    """CONSTRAINT memories are retrievable separately from vector search results.

    Constraints are injected before search results during code review — they apply
    regardless of semantic similarity score. This test verifies the query that
    powers that injection.
    """
    ns = "test:coverage:constraint-inject:" + uid()[:8]

    con_ids = []
    for i, (component, rule) in enumerate([
        ("auth-service",    "All auth tokens must be rotated every 24 hours"),
        ("api-gateway",     "Rate limit: 1000 req/min per API key, 429 on breach"),
        ("data-pipeline",   "PII fields must be masked before writing to data lake"),
    ]):
        mid = uid()
        con_ids.append(mid)
        arcade_command(
            "INSERT INTO Memory SET "
            "id = :id, content = :content, namespace = :ns, "
            "created_at = :ts, superseded_at = null, "
            "tags = ['constraint', 'security'], source = 'test', metadata = {}, "
            "memory_type = 'constraint', status = 'active', "
            "author = 'security-team', affects = [:component], "
            "rationale = 'Compliance requirement', "
            "expires_at = null, review_by = null, "
            "provenance = {}, content_embedding = []",
            {"id": mid, "content": rule, "ns": ns, "ts": now_str(), "component": component},
        )

    # Simulate constraint injection query (what the code reviewer runs first)
    constraints = arcade_query(
        "SELECT id, content, affects, rationale FROM Memory "
        "WHERE memory_type = 'constraint' "
        "  AND status = 'active' AND superseded_at IS NULL "
        "  AND (namespace = :ns OR namespace LIKE :prefix)",
        {"ns": ns, "prefix": f"{ns}:%"},
    )

    assert len(constraints) == 3, \
        f"Expected 3 constraints, got {len(constraints)}"

    for c in constraints:
        assert c.get("rationale"), \
            f"Constraint '{c.get('content', '')[:40]}' has no rationale"
        assert c.get("affects"), \
            f"Constraint '{c.get('content', '')[:40]}' has no affects[] — cannot target a component"

    # Verify auth-service constraint is retrievable by component name
    auth_constraints = [
        c for c in constraints
        if "auth-service" in [a.lower().strip() for a in (c.get("affects") or [])]
    ]
    assert len(auth_constraints) == 1, \
        "auth-service constraint not retrievable by component name"

    for mid in con_ids:
        arcade_command(
            "DELETE VERTEX FROM Memory WHERE id = :id AND namespace = :ns",
            {"id": mid, "ns": ns},
        )


def test_project_namespaces_have_decisions(runner: Runner) -> None:
    """Each configured project namespace meets its minimum decision/constraint count.

    This is the coverage gate: if a project hasn't stored its architectural decisions,
    this test fails and signals that the project's context is incomplete for code review.

    To add a new project: add it to PROJECT_NAMESPACES at the top of this file.
    """
    failures = []

    for ns, cfg in PROJECT_NAMESPACES.items():
        gov = get_governance_memories(ns)
        decisions   = [r for r in gov if r["memory_type"] in ("decision", "adr")]
        constraints = [r for r in gov if r["memory_type"] == "constraint"]

        min_dec = cfg.get("min_decisions", 1)
        min_con = cfg.get("min_constraints", 0)

        if len(decisions) < min_dec:
            failures.append(
                f"{ns}: needs {min_dec} decision(s), has {len(decisions)}"
            )
        if len(constraints) < min_con:
            failures.append(
                f"{ns}: needs {min_con} constraint(s), has {len(constraints)}"
            )

        if runner.verbose:
            print(f"\n    {ns}:")
            print(f"      decisions/ADRs: {len(decisions)}  (min {min_dec})")
            print(f"      constraints:    {len(constraints)}  (min {min_con})")
            for r in decisions[:3]:
                snippet = r.get("content", "")[:80]
                print(f"      [decision] {snippet}")

    assert not failures, (
        "Project decision coverage gaps:\n" +
        "\n".join(f"  • {f}" for f in failures) +
        "\n\nFix: write decision/constraint memories to the namespace using "
        "vault_secret_set or memory_write with memory_type='decision'."
    )


def test_decisions_have_quality_fields(runner: Runner) -> None:
    """Decisions and constraints in project namespaces have affects[] and rationale.

    A decision without affects[] cannot be targeted to a component during code review.
    A decision without rationale is just a rule with no WHY — less useful for review.

    The gate is controlled per-namespace by ``min_quality_pct`` in PROJECT_NAMESPACES.
    Set to 0.0 during bootstrapping and ratchet up as you backfill existing memories.
    At 1.0, every decision must have both fields (full enforcement).
    """
    gate_failures = []
    quality_issues: list[str] = []

    for ns, cfg in PROJECT_NAMESPACES.items():
        min_pct = cfg.get("min_quality_pct", 0.0)
        gov = get_governance_memories(ns)
        if not gov:
            continue

        good = 0
        ns_issues: list[str] = []
        for r in gov:
            mid      = r["id"][:8]
            content  = r.get("content", "")[:60]
            mtype    = r.get("memory_type", "?")
            affects  = r.get("affects") or []
            rationale = r.get("rationale") or ""

            has_affects   = bool(affects)
            has_rationale = bool(rationale.strip())

            if has_affects and has_rationale:
                good += 1
            if not has_affects:
                ns_issues.append(
                    f"[{ns}] {mtype} {mid} has no affects[] — "
                    f"cannot target a component: '{content}'"
                )
            if not has_rationale:
                ns_issues.append(
                    f"[{ns}] {mtype} {mid} has no rationale — "
                    f"WHY is missing: '{content}'"
                )

        quality_issues.extend(ns_issues)
        actual_pct = good / len(gov)
        if actual_pct < min_pct:
            gate_failures.append(
                f"{ns}: quality gate requires {min_pct:.0%} of decisions to have "
                f"affects[] + rationale, but only {actual_pct:.0%} ({good}/{len(gov)}) do"
            )
        elif runner.verbose and ns_issues:
            print(f"\n    [{ns}] {good}/{len(gov)} decisions have quality fields "
                  f"(gate={min_pct:.0%} — PASSING). Issues to fix:")
            for issue in ns_issues[:5]:
                print(f"      • {issue}")
            if len(ns_issues) > 5:
                print(f"      … and {len(ns_issues)-5} more")

    assert not gate_failures, (
        "Quality gate failed (raise min_quality_pct in PROJECT_NAMESPACES once backfilled):\n" +
        "\n".join(f"  • {f}" for f in gate_failures)
    )

    if quality_issues and runner.verbose:
        print(f"\n    ({len(quality_issues)} total quality issue(s) below gate — "
              f"run with --verbose to see, raise min_quality_pct to enforce)")


def test_graph_traversal_finds_governing_decisions(runner: Runner) -> None:
    """Graph traversal from a component name returns all governing decisions and constraints.

    Simulates the code review query: "what rules apply to <component>?"
    Writes a realistic set of decisions + constraints then verifies all are found.
    """
    ns = "test:coverage:code-review:" + uid()[:8]

    entries = [
        {
            "id": uid(),
            "mtype": "decision",
            "content": "order-service must publish domain events to Kafka — no direct inter-service HTTP",
            "affects": ["order-service", "notification-service"],
            "rationale": "Decoupling: allows services to evolve independently and improves fault isolation",
        },
        {
            "id": uid(),
            "mtype": "constraint",
            "content": "order-service must not call inventory-service synchronously during checkout",
            "affects": ["order-service"],
            "rationale": "P99 latency SLA of 200ms cannot be met if inventory check is on the critical path",
        },
        {
            "id": uid(),
            "mtype": "adr",
            "content": "ADR-007: order-service uses CQRS — write model (commands) separated from read model (queries)",
            "affects": ["order-service", "order-query-service"],
            "rationale": "Read and write access patterns differ significantly; separate models optimize each",
        },
        {
            "id": uid(),
            "mtype": "fact",  # must NOT appear in governance traversal
            "content": "order-service handles approximately 10k orders per day",
            "affects": [],
            "rationale": "",
        },
    ]

    for e in entries:
        arcade_command(
            "INSERT INTO Memory SET "
            "id = :id, content = :content, namespace = :ns, "
            "created_at = :ts, superseded_at = null, "
            "tags = [:mtype], source = 'test', metadata = {}, "
            "memory_type = :mtype, status = 'active', "
            "author = 'test', affects = :affects, rationale = :rationale, "
            "expires_at = null, review_by = null, "
            "provenance = {}, content_embedding = []",
            {
                "id": e["id"], "content": e["content"],
                "ns": ns, "ts": now_str(),
                "mtype": e["mtype"], "affects": e["affects"],
                "rationale": e["rationale"],
            },
        )

    # Code review query: find all governance entries for order-service
    gov_rows = arcade_query(
        "SELECT id, memory_type, affects, rationale FROM Memory "
        "WHERE memory_type IN ['decision', 'constraint', 'adr'] "
        "  AND status = 'active' AND superseded_at IS NULL "
        "  AND namespace = :ns",
        {"ns": ns},
    )

    gov_for_order = [
        r for r in gov_rows
        if "order-service" in [a.lower().strip() for a in (r.get("affects") or [])]
    ]

    expected_ids = {e["id"] for e in entries if e["mtype"] != "fact"}
    found_ids    = {r["id"] for r in gov_for_order}

    assert expected_ids == found_ids, (
        f"Traversal missed entries: {expected_ids - found_ids}\n"
        f"Extra entries found:      {found_ids - expected_ids}"
    )

    # fact must not be in the governance result
    fact_id = next(e["id"] for e in entries if e["mtype"] == "fact")
    assert fact_id not in found_ids, \
        "Plain fact incorrectly returned in code review governance traversal"

    # All found entries must have non-empty rationale
    for r in gov_for_order:
        assert r.get("rationale"), \
            f"Decision/constraint {r['id'][:8]} has no rationale in traversal result"

    if runner.verbose:
        print(f"\n    order-service governance ({len(gov_for_order)} entries):")
        for r in gov_for_order:
            print(f"      [{r['memory_type']}] {r['id'][:8]}")

    for e in entries:
        arcade_command(
            "DELETE VERTEX FROM Memory WHERE id = :id AND namespace = :ns",
            {"id": e["id"], "ns": ns},
        )


def test_superseded_decisions_excluded_from_code_review(runner: Runner) -> None:
    """Superseded decisions are excluded from code review traversal.

    When an architectural decision is replaced, the old one must not appear
    in the governance query — only the current version should guide reviewers.
    """
    ns = "test:coverage:superseded:" + uid()[:8]
    old_id = uid()
    new_id = uid()

    # Old (superseded) decision
    arcade_command(
        "INSERT INTO Memory SET "
        "id = :id, content = 'auth-service stores sessions in Redis (deprecated)', "
        "namespace = :ns, created_at = :ts, "
        "superseded_at = '2026-01-01 00:00:00.000', "
        "tags = ['decision'], source = 'test', metadata = {}, "
        "memory_type = 'decision', status = 'superseded', "
        "author = 'architect', affects = ['auth-service'], "
        "rationale = 'Original design — later replaced', "
        "expires_at = null, review_by = null, "
        "provenance = {}, content_embedding = []",
        {"id": old_id, "ns": ns, "ts": now_str()},
    )

    # New (active) decision
    arcade_command(
        "INSERT INTO Memory SET "
        "id = :id, content = 'auth-service stores sessions in ArcadeDB with graph edges', "
        "namespace = :ns, created_at = :ts, superseded_at = null, "
        "tags = ['decision'], source = 'test', metadata = {}, "
        "memory_type = 'decision', status = 'active', "
        "author = 'architect', affects = ['auth-service'], "
        "rationale = 'ArcadeDB unifies storage layer — eliminates Redis ops overhead', "
        "expires_at = null, review_by = null, "
        "provenance = {}, content_embedding = []",
        {"id": new_id, "ns": ns, "ts": now_str()},
    )

    gov = get_governance_memories(ns)
    ids = {r["id"] for r in gov}

    assert new_id in ids, "Active decision not returned in code review query"
    assert old_id not in ids, \
        "Superseded decision incorrectly returned — would mislead code reviewer"

    for mid in (old_id, new_id):
        arcade_command(
            "DELETE VERTEX FROM Memory WHERE id = :id AND namespace = :ns",
            {"id": mid, "ns": ns},
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

ALL_TESTS: list[tuple[str, Callable]] = [
    ("governance_types_distinct_from_facts",          test_governance_types_are_distinct_from_facts),
    ("decision_requires_affects_and_rationale",       test_decision_requires_affects_and_rationale),
    ("constraint_injectable_before_search_results",   test_constraint_is_injectable_before_search_results),
    ("project_namespaces_have_decisions",             test_project_namespaces_have_decisions),
    ("decisions_have_quality_fields",                 test_decisions_have_quality_fields),
    ("graph_traversal_finds_governing_decisions",     test_graph_traversal_finds_governing_decisions),
    ("superseded_decisions_excluded_from_code_review",test_superseded_decisions_excluded_from_code_review),
]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Decision and constraint coverage tests for engram"
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--test", metavar="NAME", help="Run only this test")
    parser.add_argument("--namespace", metavar="NS",
                        help="Override PROJECT_NAMESPACES with a single namespace (min_decisions=1)")
    args = parser.parse_args()

    if args.namespace:
        PROJECT_NAMESPACES.clear()
        PROJECT_NAMESPACES[args.namespace] = {"min_decisions": 1, "min_constraints": 0, "key_components": []}

    runner = Runner(verbose=args.verbose)

    print("\nengram Decision & Constraint Coverage Tests")
    print(f"DB:  {ARCADEDB_URL}/{DB_NAME}")
    print(f"API: {ENGRAM_API}")
    print(f"Namespaces under test: {list(PROJECT_NAMESPACES.keys())}")
    print("=" * 70)

    tests_to_run = ALL_TESTS
    if args.test:
        tests_to_run = [(n, fn) for n, fn in ALL_TESTS if n == args.test]
        if not tests_to_run:
            available = ", ".join(n for n, _ in ALL_TESTS)
            print(f"[error] Test '{args.test}' not found.\nAvailable: {available}", file=sys.stderr)
            sys.exit(1)

    print()
    for name, fn in tests_to_run:
        runner.run(name, fn)

    passed = sum(1 for r in runner.results if r.passed)
    failed = sum(1 for r in runner.results if not r.passed)
    total_ms = sum(r.elapsed_ms for r in runner.results)

    print()
    print("=" * 70)
    print(f"Results: {passed}/{len(runner.results)} passed, {failed} failed  ({total_ms:.0f}ms total)")

    if failed:
        print("\nFailed tests:")
        for r in runner.results:
            if not r.passed:
                print(f"  ✗ {r.name}: {r.message}")
        sys.exit(1)
    else:
        print("\nAll tests passed.")


if __name__ == "__main__":
    main()
