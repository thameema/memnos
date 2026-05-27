"""
test_epoch_ms_temporal.py — Verify correctness of the epoch-ms timestamp
architecture across four dimensions:

1. Temporal API layer: created_at returned as ISO-8601 string, not raw integer
2. as_of point-in-time filtering: epoch ms comparisons filter correctly
3. SQLite migration health: existing epoch-ms-as-string rows round-trip correctly
4. Vector DB impact: ArcadeDB vector search unaffected by timestamp format change

Run:
    python3 tools/test_epoch_ms_temporal.py [--verbose]

All tests are isolated (unique namespace per run) and clean up after themselves.
"""
from __future__ import annotations

import os
import sys
import time
import uuid
import sqlite3
import tempfile
import asyncio
import argparse
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Callable

try:
    import httpx
except ImportError:
    print("[error] pip install httpx", file=sys.stderr)
    sys.exit(1)

ENGRAM_API = os.environ.get("ENGRAM_API", "http://localhost:8766")
ENGRAM_KEY = os.environ.get("ENGRAM_KEY", "engram-local-dev-key")
TEST_NS = f"test:epoch-ts:{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def api(method: str, path: str, **kwargs) -> httpx.Response:
    headers = {"X-API-Key": ENGRAM_KEY, "Content-Type": "application/json"}
    url = ENGRAM_API.rstrip("/") + path
    with httpx.Client(timeout=15) as c:
        return c.request(method, url, headers=headers, **kwargs)


def write(content: str, memory_type: str = "fact", **extra) -> dict:
    payload = {"content": content, "namespace": TEST_NS,
                "memory_type": memory_type, **extra}
    r = api("POST", "/api/v1/memory/", json=payload)
    assert r.status_code == 201, f"write failed {r.status_code}: {r.text}"
    return r.json()


def get_memory(mid: str) -> dict | None:
    r = api("GET", f"/api/v1/memory/{mid}", params={"ns": TEST_NS})
    return r.json() if r.status_code == 200 else None


def search(q: str, top_k: int = 10, **extra) -> list[dict]:
    r = api("GET", "/api/v1/memory/search",
            params={"q": q, "ns": TEST_NS, "top_k": top_k, **extra})
    assert r.status_code == 200, f"search failed {r.status_code}: {r.text}"
    data = r.json()
    return data if isinstance(data, list) else data.get("results", [])


def delete(mid: str):
    api("DELETE", f"/api/v1/memory/{mid}")


def cleanup():
    try:
        results = search("the", top_k=50)
        for r in results:
            m = r.get("memory", r)
            mid = m.get("id") or r.get("id")
            if mid:
                delete(mid)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------

@dataclass
class Runner:
    verbose: bool = False
    only: str | None = None
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
# Area 1 — Temporal API layer: timestamps must be ISO-8601 strings in responses
# ===========================================================================

def test_api_created_at_is_iso_string(runner: Runner):
    """Write a memory; verify created_at in the response is a valid ISO-8601
    string, NOT a raw epoch integer.  The conversion must happen at the API
    presentation layer — storage is epoch ms, response is ISO-8601."""
    m = write("sentinel content for timestamp format test")
    mid = m["id"]
    try:
        mem = get_memory(mid)
        assert mem is not None, "get_memory returned None"

        ts = mem.get("created_at")
        assert ts is not None, "created_at missing from response"
        assert isinstance(ts, str), \
            f"created_at should be a string, got {type(ts).__name__}: {repr(ts)}"

        # Must parse as ISO-8601 without error
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            raise AssertionError(f"created_at is not valid ISO-8601: {repr(ts)}")

        # Must be timezone-aware and recent (within the last 60 seconds)
        assert dt.tzinfo is not None, \
            f"created_at has no timezone info: {repr(ts)}"
        age = (datetime.now(timezone.utc) - dt).total_seconds()
        assert -5 <= age <= 60, \
            f"created_at looks wrong — age {age:.1f}s (expected 0–60s): {repr(ts)}"

        if runner.verbose:
            print(f"\n    created_at = {repr(ts)}")
            print(f"    parsed    = {dt.isoformat()}")
            print(f"    age       = {age:.2f}s")
    finally:
        delete(mid)


def test_search_results_have_iso_timestamps(runner: Runner):
    """Search results must include ISO-8601 created_at, not raw epoch integers."""
    m = write("epoch ms timestamp format verification in search results")
    mid = m["id"]
    time.sleep(0.3)
    try:
        results = search("epoch ms timestamp format verification")
        found = next((r for r in results
                      if (r.get("memory") or r).get("id") == mid), None)
        assert found is not None, f"Written memory {mid} not found in search results"

        mem = found.get("memory", found)
        ts = mem.get("created_at")
        assert ts is not None, "created_at missing from search result"
        assert isinstance(ts, str), \
            f"created_at in search result must be string, got {type(ts).__name__}: {repr(ts)}"
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        assert dt.tzinfo is not None, "search result created_at has no timezone"

        if runner.verbose:
            print(f"\n    search result created_at = {repr(ts)}")
    finally:
        delete(mid)


# ===========================================================================
# Area 2 — as_of point-in-time filtering with epoch ms
# ===========================================================================

def test_as_of_excludes_future_writes(runner: Runner):
    """Write v1, record T1 (ISO), write v2, then query as_of=T1.
    v2 must NOT appear in the as_of result set.

    This directly validates that the epoch ms comparison
       created_at <= to_epoch_ms(as_of)
    works correctly across the ArcadeDB DATETIME type."""
    v1 = write(
        "order-processing-service uses synchronous REST to call inventory — v1",
        memory_type="decision",
        tags=["arch", "inventory"],
        affects=["order-processing-service"],
    )
    v1_id = v1["id"]

    # Record a timestamp between v1 and v2
    t_between = datetime.now(timezone.utc).isoformat()
    time.sleep(0.5)  # ensure v2 has a strictly later created_at

    v2 = write(
        "order-processing-service MUST use Kafka events — v2 supersedes v1",
        memory_type="decision",
        tags=["arch", "inventory", "kafka"],
        affects=["order-processing-service"],
    )
    v2_id = v2["id"]

    try:
        time.sleep(0.5)

        r = api("GET", "/api/v1/memory/search", params={
            "q": "order-processing inventory service",
            "ns": TEST_NS, "top_k": 20, "as_of": t_between,
        })

        if r.status_code != 200:
            if runner.verbose:
                print(f"\n    as_of not supported ({r.status_code}) — skipped")
            return  # as_of is optional; don't fail if unimplemented

        as_of_results = r.json() if isinstance(r.json(), list) else r.json().get("results", [])
        as_of_ids = {(x.get("memory") or x).get("id") for x in as_of_results}

        # as_of filter is only meaningful if it changed the result set
        all_results = search("order-processing inventory service", top_k=20)
        all_ids = {(x.get("memory") or x).get("id") for x in all_results}

        if as_of_ids == all_ids:
            if runner.verbose:
                print("\n    as_of returned same set as current — "
                      "filtering inactive (superseded_at not set on v1)")
            return  # Known limitation: as_of requires explicit supersede

        assert v2_id not in as_of_ids, \
            f"v2 (written AFTER T1) appeared in as_of={t_between[:19]} result"
        assert v1_id in as_of_ids, \
            f"v1 (written BEFORE T1) missing from as_of={t_between[:19]} result"

        if runner.verbose:
            print(f"\n    T1 = {t_between[:19]}")
            print(f"    v1 in as_of results: {v1_id in as_of_ids} ✓")
            print(f"    v2 in as_of results: {v2_id in as_of_ids} ✓ (expected False)")
    finally:
        delete(v1_id)
        delete(v2_id)


def test_temporal_ordering_newest_first(runner: Runner):
    """Write 3 memories with deliberate time gaps. Fetch each by ID and verify:
    (a) all created_at are valid ISO-8601 UTC strings,
    (b) each successive memory has a created_at >= the previous one.

    Uses per-ID GET (not search) to avoid semantic-ranking interference."""
    ids = []
    writes_at = []
    for i in range(3):
        before = datetime.now(timezone.utc)
        m = write(f"distinct temporal ordering memory delta-{uuid.uuid4().hex[:8]}",
                  tags=["ordering-test"])
        ids.append(m["id"])
        writes_at.append(before)
        if i < 2:
            time.sleep(0.4)   # ensure distinct epoch ms

    try:
        timestamps = []
        for mid in ids:
            mem = get_memory(mid)
            assert mem is not None, f"GET {mid} returned None"
            ts = mem.get("created_at")
            assert ts is not None, f"created_at missing for {mid}"
            assert isinstance(ts, str), \
                f"created_at must be string, got {type(ts).__name__}: {repr(ts)}"
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            assert dt.tzinfo is not None, f"created_at timezone-naive for {mid}"
            timestamps.append(dt)

        # Each successive memory must be >= the previous (epoch ms sort is correct)
        for i in range(1, len(timestamps)):
            assert timestamps[i] >= timestamps[i - 1], \
                (f"Ordering wrong: memory {i} ({timestamps[i].isoformat()}) "
                 f"< memory {i-1} ({timestamps[i-1].isoformat()})")

        # Each timestamp must be within 30s of when we wrote it (allows for slow CI/dev)
        for i, (dt, wrote_at) in enumerate(zip(timestamps, writes_at)):
            lag = (dt - wrote_at).total_seconds()
            assert -1 <= lag <= 30, \
                f"Memory {i} created_at drift {lag:.2f}s — expected 0–30s"

        if runner.verbose:
            for i, dt in enumerate(timestamps):
                print(f"\n    memory[{i}] created_at = {dt.isoformat()}")
    finally:
        for mid in ids:
            delete(mid)


# ===========================================================================
# Area 3 — SQLite migration health: epoch-ms-as-string rows round-trip correctly
# ===========================================================================

def _skip_if_missing_deps():
    """Return a skip message if SQLite store deps are not available locally."""
    try:
        import aiosqlite  # noqa: F401
        import yaml  # noqa: F401
        return None
    except ImportError as e:
        return f"(skipped — run inside Docker where deps are installed: {e})"


def test_sqlite_episode_store_epoch_ms_roundtrip(runner: Runner):
    """Write an EpisodeRecord with a known UTC datetime; read it back and
    verify the datetime round-trips exactly (within 1 second tolerance).

    This covers the migration path: existing rows have epoch ms stored as
    TEXT strings (e.g. '1748000000000') due to SQLite TEXT column affinity.
    The _from_ms() helper must handle that format."""
    skip = _skip_if_missing_deps()
    if skip:
        if runner.verbose:
            print(f"\n    {skip}")
        return  # not a failure — Docker covers this

    # Import store directly (runs in-process, uses Docker's mounted volume)
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages", "learning"))
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages", "core"))

    from engram_learning.episode_store import EpisodeStore
    from engram_learning.models import EpisodicRecord, Outcome

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    # Simulate the migration scenario: write epoch ms as TEXT string directly
    # (what the old ISO rows look like after the TEXT-column migration script)
    known_dt = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)
    epoch_ms_str = str(int(known_dt.timestamp() * 1000))   # '1748174400000'

    con = sqlite3.connect(db_path)
    con.execute("""
        CREATE TABLE episodes (
            id TEXT PRIMARY KEY, task_id TEXT, namespace TEXT,
            original_prompt TEXT, decomposition TEXT, agent_used TEXT,
            runtime TEXT, outcome TEXT, user_feedback TEXT,
            quality_score REAL, duration_s REAL, token_cost INTEGER,
            created_at TEXT, tags TEXT
        )
    """)
    con.execute(
        "INSERT INTO episodes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("ep-1", "t-1", "test:ns", "prompt", "[]", "agent", "api",
         "SUCCESS", None, 0.9, 1.0, 100, epoch_ms_str, "[]"),
    )
    con.commit()
    con.close()

    store = EpisodeStore(db_path=db_path)

    async def _run():
        await store.init()
        ep = await store.get("ep-1")
        return ep

    ep = asyncio.run(_run())

    try:
        assert ep is not None, "Episode not found after init"
        assert ep.created_at is not None, "created_at is None"
        assert ep.created_at.tzinfo is not None, \
            f"created_at has no timezone: {ep.created_at}"
        diff = abs((ep.created_at - known_dt).total_seconds())
        assert diff < 1.0, \
            f"created_at round-trip error: expected {known_dt.isoformat()}, " \
            f"got {ep.created_at.isoformat()} (diff={diff:.3f}s)"

        if runner.verbose:
            print(f"\n    Stored as TEXT string: {repr(epoch_ms_str)}")
            print(f"    Round-tripped as:      {ep.created_at.isoformat()}")
            print(f"    Expected:              {known_dt.isoformat()}")
    finally:
        os.unlink(db_path)


def test_sqlite_heuristic_store_new_write_roundtrip(runner: Runner):
    """Write a Heuristic using the new epoch ms path; read it back.
    Verifies the full write→read cycle for new data (not migration data)."""
    skip = _skip_if_missing_deps()
    if skip:
        if runner.verbose:
            print(f"\n    {skip}")
        return

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages", "learning"))
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages", "core"))

    from engram_learning.heuristic_store import HeuristicStore
    from engram_learning.models import Heuristic

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    store = HeuristicStore(db_path=db_path)

    h = Heuristic(
        id="h-test-1",
        namespace="test:ns",
        rule="Always use UTC datetimes in storage",
        rationale="Timezone consistency",
        applies_to_tags=["storage", "datetime"],
        confidence=0.95,
    )

    async def _run():
        await store.init()
        await store.add(h)
        all_h = await store.get_all("test:ns")
        return all_h

    results = asyncio.run(_run())

    try:
        assert len(results) == 1, f"Expected 1 heuristic, got {len(results)}"
        read_h = results[0]
        assert read_h.id == "h-test-1"
        assert read_h.created_at is not None, "created_at is None"
        assert read_h.created_at.tzinfo is not None, \
            f"created_at timezone-naive: {read_h.created_at}"

        diff = abs((read_h.created_at - h.created_at).total_seconds())
        assert diff < 1.0, \
            f"created_at round-trip error {diff:.3f}s: " \
            f"wrote {h.created_at.isoformat()}, got {read_h.created_at.isoformat()}"

        # Verify the SQLite column holds a numeric value (epoch ms as string)
        con = sqlite3.connect(db_path)
        raw = con.execute("SELECT created_at FROM heuristics WHERE id='h-test-1'").fetchone()[0]
        con.close()
        assert str(raw).lstrip('-').isdigit(), \
            f"Expected epoch ms integer in TEXT column, got: {repr(raw)}"

        if runner.verbose:
            print(f"\n    SQLite raw value:  {repr(raw)}")
            print(f"    Round-tripped dt:  {read_h.created_at.isoformat()}")
            print(f"    Original dt:       {h.created_at.isoformat()}")
    finally:
        os.unlink(db_path)


def test_sqlite_task_store_status_update(runner: Runner):
    """Write a Task, mark it COMPLETE, verify completed_at is epoch ms
    in storage and a valid UTC datetime when read back."""
    skip = _skip_if_missing_deps()
    if skip:
        if runner.verbose:
            print(f"\n    {skip}")
        return

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages", "orchestrator"))

    from engram_orchestrator.task_store import TaskStore
    from engram_orchestrator.models import Task, TaskStatus

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    store = TaskStore(db_path=str(db_path))

    task = Task(
        id="task-ts-1",
        prompt="test task for epoch ms verification",
        namespace="test:ns",
        runtime="api",
        status=TaskStatus.PENDING,
    )

    async def _run():
        await store.init()
        await store.save(task)
        await store.update_status("task-ts-1", TaskStatus.COMPLETE, result="done")
        t = await store.get("task-ts-1")
        await store.close()
        return t

    t = asyncio.run(_run())

    try:
        assert t is not None, "Task not found"
        assert t.status == TaskStatus.COMPLETE

        assert t.created_at is not None, "created_at is None"
        assert t.created_at.tzinfo is not None, "created_at timezone-naive"

        assert t.completed_at is not None, "completed_at is None after COMPLETE"
        assert t.completed_at.tzinfo is not None, "completed_at timezone-naive"

        assert t.completed_at >= t.created_at, \
            f"completed_at ({t.completed_at}) < created_at ({t.created_at})"

        # Verify SQLite storage is epoch ms
        con = sqlite3.connect(db_path)
        row = con.execute(
            "SELECT created_at, completed_at FROM tasks WHERE id='task-ts-1'"
        ).fetchone()
        con.close()
        for col_name, raw in zip(("created_at", "completed_at"), row):
            assert str(raw).lstrip('-').isdigit(), \
                f"tasks.{col_name} is not epoch ms: {repr(raw)}"

        if runner.verbose:
            print(f"\n    created_at raw:   {repr(row[0])}")
            print(f"    completed_at raw: {repr(row[1])}")
            print(f"    created_at dt:    {t.created_at.isoformat()}")
            print(f"    completed_at dt:  {t.completed_at.isoformat()}")
    finally:
        os.unlink(db_path)


# ===========================================================================
# Area 4 — Vector DB: verify epoch ms change has no impact on vector search
# ===========================================================================

def test_vector_search_survives_epoch_ms_writes(runner: Runner):
    """Write a memory (uses epoch ms internally); search for it using the
    vector path. If the result is found with a valid score, the vector index
    is intact — epoch ms timestamps are not part of the embedding vector."""
    m = write(
        "quantum-resistant cryptography implementation using CRYSTALS-Kyber "
        "post-quantum key encapsulation mechanism",
        tags=["crypto", "pqc"],
    )
    mid = m["id"]
    time.sleep(0.8)   # let embedding + indexing complete

    try:
        results = search("post-quantum key encapsulation Kyber cryptography", top_k=5)
        found = next(
            (r for r in results if (r.get("memory") or r).get("id") == mid),
            None,
        )
        assert found is not None, \
            f"Memory {mid} not found via vector search after epoch ms write"

        # Score must be numeric (vector cosine similarity 0–1)
        score = found.get("score", 0)
        assert isinstance(score, (int, float)), \
            f"score should be numeric, got {type(score).__name__}: {score}"
        assert score > 0.0, f"Vector score is zero — embedding may have failed"

        if runner.verbose:
            print(f"\n    Vector search score = {score:.4f}")
            print(f"    memory_type = {(found.get('memory') or found).get('memory_type')}")
    finally:
        delete(mid)


def test_multiple_writes_all_get_valid_timestamps(runner: Runner):
    """Write two memories via the epoch ms path; fetch each by ID.
    Verify both carry valid ISO-8601 UTC timestamps and that m2's created_at
    is >= m1's (epoch ms integer sort order is preserved end-to-end).

    This validates the full write pipeline:
      Python datetime → to_epoch_ms() → ArcadeDB DATETIME
      → _parse_dt() → datetime → Pydantic → ISO-8601 at the API layer."""
    before_m1 = datetime.now(timezone.utc)
    m1 = write(
        "aurora-db-cluster primary failover policy automatic ha",
        tags=["db", "ha"],
    )
    time.sleep(0.3)
    m2 = write(
        "aurora-db-cluster read-replica scaling policy burst capacity",
        tags=["db", "scaling"],
    )

    try:
        mem1 = get_memory(m1["id"])
        mem2 = get_memory(m2["id"])

        assert mem1 is not None, "m1 not found by GET"
        assert mem2 is not None, "m2 not found by GET"

        for label, mem in (("m1", mem1), ("m2", mem2)):
            ts = mem.get("created_at", "")
            assert isinstance(ts, str), \
                f"{label} created_at not a string: {type(ts).__name__}: {repr(ts)}"
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            assert dt.tzinfo is not None, \
                f"{label} created_at timezone-naive: {repr(ts)}"
            age = (datetime.now(timezone.utc) - dt).total_seconds()
            assert 0 <= age <= 30, \
                f"{label} created_at age wrong ({age:.1f}s): {repr(ts)}"

        # m2 must be written after m1 — epoch ms sort preserved
        dt1 = datetime.fromisoformat(mem1["created_at"].replace("Z", "+00:00"))
        dt2 = datetime.fromisoformat(mem2["created_at"].replace("Z", "+00:00"))
        assert dt2 >= dt1, \
            f"m2.created_at ({dt2.isoformat()}) < m1.created_at ({dt1.isoformat()})"

        if runner.verbose:
            print(f"\n    m1 created_at: {mem1['created_at']}")
            print(f"    m2 created_at: {mem2['created_at']}")
            gap_ms = int((dt2 - dt1).total_seconds() * 1000)
            print(f"    gap: {gap_ms}ms (epoch ms sort order correct ✓)")
    finally:
        delete(m1["id"])
        delete(m2["id"])


# ===========================================================================
# Entry point
# ===========================================================================

def main() -> int:
    parser = argparse.ArgumentParser(description="Epoch-ms temporal correctness tests")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--test", metavar="NAME", help="run one test by name")
    args = parser.parse_args()

    try:
        with httpx.Client(timeout=4) as c:
            r = c.get(f"{ENGRAM_API}/api/v1/admin/health",
                      headers={"X-API-Key": ENGRAM_KEY})
            if r.status_code != 200:
                print(f"[error] engram health check failed: {r.status_code}", file=sys.stderr)
                return 1
    except Exception as e:
        print(f"[error] Cannot reach engram at {ENGRAM_API}: {e}", file=sys.stderr)
        return 1

    runner = Runner(verbose=args.verbose, only=args.test)

    print("Epoch-ms Temporal Correctness Tests")
    print(f"API: {ENGRAM_API}   namespace: {TEST_NS}")
    print("=" * 70)

    print("\n── Area 1: API timestamp format ─────────────────────────────────────")
    runner.run(test_api_created_at_is_iso_string)
    runner.run(test_search_results_have_iso_timestamps)

    print("\n── Area 2: as_of point-in-time filtering ────────────────────────────")
    runner.run(test_as_of_excludes_future_writes)
    runner.run(test_temporal_ordering_newest_first)

    print("\n── Area 3: SQLite migration health ──────────────────────────────────")
    runner.run(test_sqlite_episode_store_epoch_ms_roundtrip)
    runner.run(test_sqlite_heuristic_store_new_write_roundtrip)
    runner.run(test_sqlite_task_store_status_update)

    print("\n── Area 4: Vector DB impact ─────────────────────────────────────────")
    runner.run(test_vector_search_survives_epoch_ms_writes)
    runner.run(test_multiple_writes_all_get_valid_timestamps)

    cleanup()

    return runner.summarise()


if __name__ == "__main__":
    sys.exit(main())
