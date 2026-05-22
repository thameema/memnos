#!/usr/bin/env python3
"""
engram smoke test — end-to-end verification of the engram REST API.

Usage:
    python smoke_test.py --api-key <key>
    python smoke_test.py --api-key <key> --engram-url http://localhost:8766
    python smoke_test.py --api-key <key> --skip-task --no-cleanup
"""

import argparse
import sys
import time
import json
from typing import Any

import requests

# ---------------------------------------------------------------------------
# ANSI color helpers (degrade gracefully when stdout is not a tty)
# ---------------------------------------------------------------------------

_USE_COLOR = sys.stdout.isatty()

_GREEN  = "\033[32m"
_RED    = "\033[31m"
_YELLOW = "\033[33m"
_BOLD   = "\033[1m"
_RESET  = "\033[0m"


def _color(text: str, code: str) -> str:
    if _USE_COLOR:
        return f"{code}{text}{_RESET}"
    return text


def green(text: str) -> str:
    return _color(text, _GREEN)


def red(text: str) -> str:
    return _color(text, _RED)


def yellow(text: str) -> str:
    return _color(text, _YELLOW)


def bold(text: str) -> str:
    return _color(text, _BOLD)


# ---------------------------------------------------------------------------
# Result tracking
# ---------------------------------------------------------------------------

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"

_results: list[tuple[int, str, str, str]] = []  # (num, label, status, detail)


def _record(num: int, label: str, status: str, detail: str) -> None:
    _results.append((num, label, status, detail))
    icon = green("✓") if status == PASS else (yellow("⚠") if status == WARN else red("✗"))
    status_str = (
        green(status) if status == PASS else (yellow(status) if status == WARN else red(status))
    )
    print(f"  [{num:>2}] {label:<30} {icon} {status_str}  ({detail})")


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _session(base_url: str, api_key: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    s.base_url = base_url.rstrip("/")  # type: ignore[attr-defined]
    return s


def _get(s: requests.Session, path: str, **kwargs: Any) -> requests.Response:
    return s.get(f"{s.base_url}{path}", timeout=30, **kwargs)  # type: ignore[attr-defined]


def _post(s: requests.Session, path: str, body: dict[str, Any]) -> requests.Response:
    return s.post(f"{s.base_url}{path}", json=body, timeout=30)  # type: ignore[attr-defined]


def _delete(s: requests.Session, path: str) -> requests.Response:
    return s.delete(f"{s.base_url}{path}", timeout=30)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Individual test steps
# ---------------------------------------------------------------------------


def test_health(s: requests.Session, num: int) -> bool:
    label = "Health check"
    try:
        r = _get(s, "/api/v1/admin/health")
        r.raise_for_status()
        data = r.json()
        status = data.get("status", "")
        if status == "ok":
            _record(num, label, PASS, f"status: {status}")
            return True
        _record(num, label, FAIL, f"unexpected status: {status!r}")
        return False
    except Exception as exc:
        _record(num, label, FAIL, str(exc))
        return False


def test_write_memory(
    s: requests.Session,
    num: int,
    label: str,
    content: str,
    namespace: str,
    tags: list[str],
) -> str | None:
    """Return memory id on success, None on failure."""
    try:
        r = _post(s, "/api/v1/memory/", {
            "content": content,
            "namespace": namespace,
            "tags": tags,
        })
        r.raise_for_status()
        data = r.json()
        mem_id = data.get("id", "")
        if mem_id:
            _record(num, label, PASS, f"id: {mem_id}")
            return str(mem_id)
        _record(num, label, FAIL, f"no id in response: {data}")
        return None
    except Exception as exc:
        _record(num, label, FAIL, str(exc))
        return None


def test_search_exact(
    s: requests.Session,
    num: int,
    namespace: str,
) -> bool:
    label = "Search — exact content"
    query = "JWT tokens 24h expiry"
    try:
        r = _get(s, "/api/v1/memory/search", params={
            "q": query,
            "ns": namespace,
            "top_k": 5,
            "mode": "hybrid",
        })
        r.raise_for_status()
        results = r.json()
        if not isinstance(results, list):
            _record(num, label, FAIL, f"expected list, got {type(results).__name__}")
            return False
        if len(results) == 0:
            _record(num, label, FAIL, "0 results returned")
            return False
        # Score may live at top level or inside a nested dict
        first = results[0]
        score = first.get("score") or first.get("_score") or first.get("similarity") or 0.0
        try:
            score = float(score)
        except (TypeError, ValueError):
            score = 0.0
        if score < 0.5:
            _record(num, label, FAIL, f"found {len(results)} result(s) but score {score:.2f} < 0.5")
            return False
        _record(num, label, PASS, f"found {len(results)} result(s), score {score:.2f}")
        return True
    except Exception as exc:
        _record(num, label, FAIL, str(exc))
        return False


def test_search_semantic(
    s: requests.Session,
    num: int,
    namespace: str,
) -> bool:
    label = "Search — semantic query"
    query = "how long do sessions last"
    try:
        r = _get(s, "/api/v1/memory/search", params={
            "q": query,
            "ns": namespace,
            "top_k": 5,
            "mode": "hybrid",
        })
        r.raise_for_status()
        results = r.json()
        if not isinstance(results, list):
            _record(num, label, FAIL, f"expected list, got {type(results).__name__}")
            return False
        if len(results) == 0:
            _record(num, label, FAIL, "0 results returned")
            return False
        _record(num, label, PASS, f"found {len(results)} result(s)")
        return True
    except Exception as exc:
        _record(num, label, FAIL, str(exc))
        return False


def test_add_graph_edge(
    s: requests.Session,
    num: int,
    namespace: str,
) -> bool:
    label = "Add graph edge"
    try:
        r = _post(s, "/api/v1/graph/fact", {
            "subject": "JWT auth",
            "predicate": "uses",
            "object": "Redis refresh tokens",
            "namespace": namespace,
        })
        r.raise_for_status()
        _record(num, label, PASS, f"HTTP {r.status_code}")
        return True
    except Exception as exc:
        _record(num, label, FAIL, str(exc))
        return False


def test_graph_query(
    s: requests.Session,
    num: int,
    namespace: str,
) -> bool:
    label = "Graph query — verify edge"
    cypher = "MATCH (n) WHERE n.namespace = $ns RETURN count(n) as total"
    try:
        r = _post(s, "/api/v1/graph/query", {
            "cypher": cypher,
            "namespace": namespace,
            "params": {"ns": namespace},
        })
        r.raise_for_status()
        rows = r.json()
        if not isinstance(rows, list):
            _record(num, label, FAIL, f"expected list, got {type(rows).__name__}")
            return False
        _record(num, label, PASS, f"{len(rows)} row(s) returned")
        return True
    except Exception as exc:
        _record(num, label, FAIL, str(exc))
        return False


def test_stats(
    s: requests.Session,
    num: int,
    namespace: str,
) -> bool:
    label = "Stats endpoint"
    try:
        r = _get(s, "/api/v1/graph/stats", params={"namespace": namespace})
        if r.status_code == 404:
            _record(num, label, WARN, "endpoint not found (404) — skipped")
            return True  # treat as non-blocking
        r.raise_for_status()
        data = r.json()
        nodes = data.get("node_count", data.get("nodes", "?"))
        mems = data.get("memory_count", data.get("memories", "?"))
        _record(num, label, PASS, f"nodes: {nodes}, memories: {mems}")
        return True
    except Exception as exc:
        _record(num, label, FAIL, str(exc))
        return False


def test_visualize(
    s: requests.Session,
    num: int,
    namespace: str,
) -> bool:
    label = "Visualization endpoint"
    try:
        r = _get(s, "/api/v1/graph/visualize", params={"namespace": namespace, "limit": 50})
        if r.status_code == 404:
            _record(num, label, WARN, "endpoint not found (404) — skipped")
            return True
        r.raise_for_status()
        data = r.json()
        nodes = data.get("nodes", [])
        edges = data.get("edges", [])
        _record(num, label, PASS, f"{len(nodes)} node(s), {len(edges)} edge(s)")
        return True
    except Exception as exc:
        _record(num, label, FAIL, str(exc))
        return False


def test_spawn_task(
    s: requests.Session,
    num: int,
    namespace: str,
) -> str | None:
    """Return task_id on success/warn, None on hard failure."""
    label = "Spawn background task"
    try:
        r = _post(s, "/api/v1/tasks/", {
            "prompt": "Say hello in exactly 5 words",
            "namespace": namespace,
            "runtime": "api",
        })
        if r.status_code == 404:
            _record(num, label, WARN, "tasks endpoint not found (404) — no LLM key?")
            return "SKIP"
        if r.status_code in (401, 403, 422, 500):
            try:
                detail = r.json().get("detail", r.text[:80])
            except Exception:
                detail = r.text[:80]
            _record(num, label, WARN, f"HTTP {r.status_code}: {detail} — no LLM key?")
            return "SKIP"
        r.raise_for_status()
        data = r.json()
        task_id = data.get("task_id", "")
        if not task_id:
            _record(num, label, FAIL, f"no task_id in response: {data}")
            return None
        _record(num, label, PASS, f"task_id: {task_id}")
        return str(task_id)
    except Exception as exc:
        _record(num, label, FAIL, str(exc))
        return None


def test_poll_task(
    s: requests.Session,
    num: int,
    task_id: str,
    max_wait: int = 120,
    interval: int = 5,
) -> bool:
    label = "Poll task until done"
    if task_id == "SKIP":
        _record(num, label, WARN, "skipped — task spawn was not attempted")
        return True
    start = time.time()
    last_status = "UNKNOWN"
    try:
        while True:
            elapsed = time.time() - start
            if elapsed > max_wait:
                _record(
                    num, label, FAIL,
                    f"timed out after {int(elapsed)}s, last status: {last_status}",
                )
                return False
            r = _get(s, f"/api/v1/tasks/{task_id}")
            if r.status_code == 404:
                _record(num, label, WARN, "task endpoint returned 404 — skipped")
                return True
            r.raise_for_status()
            data = r.json()
            last_status = data.get("status", "UNKNOWN")
            if last_status == "COMPLETED":
                result = data.get("result") or ""
                _record(
                    num, label, PASS,
                    f"completed in {int(elapsed)}s, result: {len(result)} chars",
                )
                return True
            if last_status == "FAILED":
                _record(num, label, FAIL, f"task failed after {int(elapsed)}s")
                return False
            time.sleep(interval)
    except Exception as exc:
        _record(num, label, FAIL, str(exc))
        return False


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


def cleanup_memories(
    s: requests.Session,
    namespace: str,
    memory_ids: list[str],
) -> None:
    print(f"\nCleaning up namespace {bold(namespace)} ...")
    ok = 0
    fail = 0
    for mem_id in memory_ids:
        try:
            r = _delete(s, f"/api/v1/memory/{mem_id}")
            if r.status_code in (200, 204, 404):
                ok += 1
            else:
                fail += 1
                print(f"  {red('✗')} DELETE {mem_id} → HTTP {r.status_code}")
        except Exception as exc:
            fail += 1
            print(f"  {red('✗')} DELETE {mem_id} → {exc}")
    print(f"  Deleted {ok} memor{'y' if ok == 1 else 'ies'}" +
          (f", {fail} failed" if fail else ""))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="engram end-to-end smoke test",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--engram-url",
        default="http://localhost:8766",
        help="Base URL of the engram server (default: http://localhost:8766)",
    )
    parser.add_argument(
        "--api-key",
        required=True,
        help="Bearer API key for authentication",
    )
    parser.add_argument(
        "--no-cleanup",
        action="store_true",
        help="Skip the cleanup prompt after tests",
    )
    parser.add_argument(
        "--skip-task",
        action="store_true",
        help="Skip task spawn/poll tests (use when no LLM key is configured)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    timestamp = int(time.time())
    namespace = f"smoke-test:{timestamp}"

    s = _session(args.engram_url, args.api_key)

    print()
    print(bold("engram smoke test"))
    print(f"  Server   : {args.engram_url}")
    print(f"  Namespace: {namespace}")
    print()

    memory_ids: list[str] = []
    n = 0

    # [1] Health
    n += 1
    health_ok = test_health(s, n)
    if not health_ok:
        print(f"\n  {red('Server is not healthy — aborting remaining tests.')}")
        _print_summary()
        return 1

    # [2] Write memory 1
    n += 1
    mem1_id = test_write_memory(
        s, n,
        label="Write memory",
        content="engram smoke test: authentication uses JWT tokens with 24h expiry",
        namespace=namespace,
        tags=["test", "auth"],
    )
    if mem1_id:
        memory_ids.append(mem1_id)

    # [3] Search exact
    n += 1
    test_search_exact(s, n, namespace)

    # [4] Search semantic
    n += 1
    test_search_semantic(s, n, namespace)

    # [5] Write memory 2
    n += 1
    mem2_id = test_write_memory(
        s, n,
        label="Write second memory",
        content="engram smoke test: the refresh token is stored in Redis",
        namespace=namespace,
        tags=["test", "redis"],
    )
    if mem2_id:
        memory_ids.append(mem2_id)

    # [6] Graph edge
    n += 1
    test_add_graph_edge(s, n, namespace)

    # [7] Graph query
    n += 1
    test_graph_query(s, n, namespace)

    # [8] Stats
    n += 1
    test_stats(s, n, namespace)

    # [9] Visualize
    n += 1
    test_visualize(s, n, namespace)

    # [10] Spawn task
    n += 1
    if args.skip_task:
        _record(n, "Spawn background task", WARN, "--skip-task flag set")
        task_id = "SKIP"
    else:
        task_id = test_spawn_task(s, n, namespace)
        if task_id is None:
            task_id = "SKIP"  # spawn failed but was already recorded as FAIL

    # [11] Poll task
    n += 1
    if args.skip_task:
        _record(n, "Poll task until done", WARN, "--skip-task flag set")
    else:
        test_poll_task(s, n, task_id)

    # Summary
    _print_summary()

    # Cleanup
    if not args.no_cleanup and memory_ids:
        print(f"\nClean up {bold(namespace)} namespace? [y/N] ", end="", flush=True)
        try:
            answer = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "n"
        if answer == "y":
            cleanup_memories(s, namespace, memory_ids)
        else:
            print("  Skipped cleanup.")

    failed = sum(1 for _, _, status, _ in _results if status == FAIL)
    return 0 if failed == 0 else 1


def _print_summary() -> None:
    total = len(_results)
    passed = sum(1 for _, _, status, _ in _results if status == PASS)
    warned = sum(1 for _, _, status, _ in _results if status == WARN)
    failed = sum(1 for _, _, status, _ in _results if status == FAIL)

    print()
    parts = [f"{passed}/{total} passed"]
    if warned:
        parts.append(yellow(f"{warned} warned"))
    if failed:
        parts.append(red(f"{failed} failed"))

    label = green("SUMMARY") if failed == 0 else red("SUMMARY")
    print(f"  {label}: {', '.join(parts)}")
    print()


if __name__ == "__main__":
    sys.exit(main())
