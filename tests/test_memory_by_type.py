"""`POST /memory/by_type` (issue #193): exact, non-ranked enumeration of every memory
tagged `type` — the plain indexed lookup `core/store.py`'s list_raw_turns_by_type adds,
NOT /recall.

Why this exists, concretely: /recall's near-duplicate collapse (core/service.py's
_dedup_candidates, issue #2 — correct and wanted for recall's own job of surfacing the
most relevant handful of memories) was found, empirically, to collapse several
near-identical typed writes down to a SINGLE /recall result even with a large
raw_quota — proven while building Tommy's structured handoffs (#193), which need to see
every state-transition write for a handoff, not just whichever one recall's dedupe
happened to keep. /memory/by_type is the fix: a plain `WHERE namespace=... AND
memory_type=...` scan (the pre-existing raw_mtype partial index), no ranking at all.

Covers:
  - exact enumeration of N near-identical typed writes, oldest-first, none collapsed
  - `limit` is honored and clamped (1..1000)
  - an unknown `type` is rejected (400) — same ALLOWED_MEMORY_TYPES validation /remember
    uses, not a separate/looser check
  - `type` is required
  - namespace isolation: a write in a DIFFERENT namespace never leaks into this one's
    results, even with the same type
  - type isolation WITHIN one namespace: a 'fact' write never shows up in a 'handoff'
    query and vice versa
  - CLI/SDK: memnos_sdk.MemnosClient.memories_by_type() round-trips against the live
    server

Run: MEMNOS_DSN=... MEMNOS_URL=... python tests/test_memory_by_type.py
"""
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sdk"))

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
URL = os.environ.get("MEMNOS_URL", "http://127.0.0.1:8900")
NS = "test:memory_by_type"
NS2 = "test:memory_by_type:other_ns"
PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if (detail and not cond) else ""))
    PASS += bool(cond); FAIL += (not cond)


def call(path, token=None, body=None):
    req = urllib.request.Request(
        URL + path, method="POST",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json",
                 **({"Authorization": "Bearer " + token} if token else {})})
    try:
        r = urllib.request.urlopen(req, timeout=25)
        return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def cli(*args, token=None):
    env = dict(os.environ, MEMNOS_DSN=DSN, MEMNOS_URL=URL)
    if token:
        env["MEMNOS_TOKEN"] = token
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    cmd = [sys.executable, os.path.join(root, "memnos_cli.py"), *args]
    r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=30)
    return r.returncode, r.stdout + r.stderr


def _token_from(out):
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("mnk_"):
            return s
    raise RuntimeError(f"no mnk_ token found in output:\n{out}")


def admin_token():
    rc, out = cli("admin")
    return _token_from(out)


def main():
    admin = admin_token()
    for ns in (NS, NS2):
        cli("namespace", "add", ns, token=admin)
    cli("principal", "create", "mbt-tester", token=admin)
    rc, out = cli("token", "mint", "mbt-tester", "--label", "memory-by-type-test", token=admin)
    tok = _token_from(out)
    for ns in (NS, NS2):
        cli("grant", "add", "mbt-tester", ns, token=admin)

    print("=== N near-identical typed writes, all enumerated (none collapsed) ===")
    for i in range(5):
        status, body = call("/remember", tok, {
            "namespace": NS, "text": f"handoff abc123: hop {i}",
            "type": "handoff", "extract": False})
        check(f"write {i} succeeds", status == 200, (status, body))

    status, body = call("/memory/by_type", tok, {"namespace": NS, "type": "handoff"})
    check("200 OK", status == 200, (status, body))
    mems = body.get("memories", [])
    check("all 5 near-identical writes come back, none collapsed", len(mems) == 5, len(mems))
    check("oldest-first (ascending id)", mems == sorted(mems, key=lambda m: m["id"]), mems)
    check("every row carries content", all(m.get("content") for m in mems), mems)

    print("=== limit is honored ===")
    status, body = call("/memory/by_type", tok, {"namespace": NS, "type": "handoff", "limit": 2})
    check("200 OK", status == 200, (status, body))
    check("limit=2 returns exactly 2", len(body.get("memories", [])) == 2, body)

    print("=== unknown type rejected (same validation /remember uses) ===")
    status, body = call("/memory/by_type", tok, {"namespace": NS, "type": "not_a_real_type"})
    check("400 on unknown type", status == 400, (status, body))

    print("=== type is required ===")
    status, body = call("/memory/by_type", tok, {"namespace": NS})
    check("400 when type omitted", status == 400, (status, body))

    print("=== namespace isolation: a write elsewhere never leaks in ===")
    status, body = call("/remember", tok, {
        "namespace": NS2, "text": "handoff abc123: a write in a DIFFERENT namespace",
        "type": "handoff", "extract": False})
    check("cross-namespace write succeeds", status == 200, (status, body))
    status, body = call("/memory/by_type", tok, {"namespace": NS, "type": "handoff"})
    mems = body.get("memories", [])
    check("still exactly 5 in NS — the NS2 write did not leak in",
          len(mems) == 5, len(mems))
    check("none of NS's results mention the NS2-only text",
          not any("DIFFERENT namespace" in (m.get("content") or "") for m in mems), mems)

    print("=== type isolation within one namespace ===")
    status, body = call("/remember", tok, {
        "namespace": NS, "text": "a plain fact, not a handoff", "type": "fact", "extract": False})
    check("fact-typed write succeeds", status == 200, (status, body))
    status, body = call("/memory/by_type", tok, {"namespace": NS, "type": "fact"})
    mems = body.get("memories", [])
    check("querying type=fact returns only the fact write, not the 5 handoff ones",
          len(mems) == 1 and "plain fact" in mems[0].get("content", ""), mems)
    status, body = call("/memory/by_type", tok, {"namespace": NS, "type": "handoff"})
    check("querying type=handoff is unaffected by the fact write (still 5)",
          len(body.get("memories", [])) == 5, body)

    print("=== SDK: memories_by_type() round-trips ===")
    from memnos_sdk import MemnosClient
    client = MemnosClient(base_url=URL, token=tok, namespace=NS)
    result = client.memories_by_type("handoff")
    check("SDK call succeeds", len(result.get("memories", [])) == 5, result)

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
