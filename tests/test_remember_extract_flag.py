"""`extract` on /remember (issue #193): an explicit, optional opt-out of LLM fact
extraction for a single write — default True (unchanged behavior for every existing
caller). false takes the SAME code path a local-mode server (no LLM configured) already
takes for every write today: raw turn stored + audited, 0 facts, no model call. This is
what lets a caller get a fast, deterministic write with no extraction cost or
nondeterminism — e.g. Tommy's handoff-state transitions (#193), which fire often and
don't want an LLM re-interpreting "accepted" into something else.

Also covers: `type: "handoff"` — the new ALLOWED_MEMORY_TYPES entry, a plain queryable
tag with no pinning/bypass behavior of its own (unlike 'constraint', which gets both).

Run: MEMNOS_DSN=... MEMNOS_URL=... python tests/test_remember_extract_flag.py
"""
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
URL = os.environ.get("MEMNOS_URL", "http://127.0.0.1:8900")
NS = "test:extract_flag"
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
    import subprocess
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
        if "mnk_" in s:
            return s[s.index("mnk_"):].split()[0]
    raise RuntimeError(f"no mnk_ token found in output:\n{out}")


def admin_token():
    rc, out = cli("admin")
    return _token_from(out)


def main():
    admin = admin_token()
    cli("namespace", "add", NS, token=admin)
    cli("principal", "create", "tester", token=admin)
    rc, out = cli("token", "mint", "tester", "--label", "extract-flag-test", token=admin)
    tok = _token_from(out)
    cli("grant", "add", "tester", NS, token=admin)

    print("=== extract: false skips extraction, stores the raw turn, 0 facts ===")
    status, body = call("/remember", tok, {
        "namespace": NS, "text": "handoff deadbeef1234: accepted by codex-worker-2",
        "type": "handoff", "extract": False})
    check("200 OK", status == 200, (status, body))
    check("facts == 0 (extraction skipped)", body.get("facts") == 0, body)
    check("turn_id present (raw turn still stored)", bool(body.get("turn_id")), body)

    print("=== the stored turn is actually retrievable (not silently dropped) ===")
    status, body = call("/recall", tok, {"namespace": NS, "query": "deadbeef1234 handoff status"})
    check("200 OK", status == 200, (status, body))
    mems = body.get("memories", [])
    check("the handoff turn comes back on recall",
          any("deadbeef1234" in (m.get("content") or "") for m in mems),
          [m.get("content") for m in mems])

    print("=== type: handoff is accepted (new ALLOWED_MEMORY_TYPES entry) ===")
    status, body = call("/remember", tok, {
        "namespace": NS, "text": "handoff deadbeef1234: in_progress",
        "type": "handoff", "extract": False})
    check("200 OK, handoff type accepted", status == 200, (status, body))

    print("=== type: handoff does NOT get pinned into /recall (unlike constraint) ===")
    status, body = call("/recall", tok, {"namespace": NS, "query": "unrelated query about something else entirely"})
    check("200 OK", status == 200, (status, body))
    mems = body.get("memories", [])
    check("no handoff-typed memory is force-pinned on an unrelated query",
          not any(m.get("pinned") and m.get("type") == "handoff" for m in mems),
          mems)

    print("=== extract omitted (default True) still behaves exactly as before ===")
    status, body = call("/remember", tok, {
        "namespace": NS, "text": "a completely ordinary memory with no extract field at all"})
    check("200 OK, unchanged default behavior", status == 200, (status, body))
    check("facts key present (0 here since this server has no LLM configured — "
          "same as every pre-existing local-mode write, untouched by this change)",
          "facts" in body, body)

    print("=== still rejects an unknown type (400) — validation untouched ===")
    status, body = call("/remember", tok, {
        "namespace": NS, "text": "x", "type": "not_a_real_type"})
    check("400 on unknown type", status == 400, (status, body))
    check("error message still lists allowed types including handoff",
          "handoff" in json.dumps(body), body)

    print("=== CLI: remember --type handoff --no-extract round-trips ===")
    rc, out = cli("remember", "a cli-written handoff marker deadbeefCLI",
                  "--namespace", NS, "--type", "handoff", "--no-extract", token=tok)
    check("CLI exits 0", rc == 0, out)
    check("CLI reports it landed in the namespace", f"remembered in {NS}" in out, out)
    rc, out = cli("recall", "deadbeefCLI", "--namespace", NS, token=tok)
    check("CLI recall finds the handoff marker written via --no-extract",
          "deadbeefCLI" in out, out)
    rc, out = cli("remember", "bad type via cli", "--namespace", NS,
                  "--type", "not_a_real_type", token=tok)
    check("CLI rejects an unknown --type at argparse level (nonzero exit)", rc != 0)

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
