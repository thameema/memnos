"""No-AI tests for the server-side namespace binding registry (issue #20, Part A).
Pure HTTP + control-plane + the local resolver — NO LLM, NO embeddings, $0.

Run against a live local server (same harness as the rest of tests/):
    MEMNOS_DSN=... MEMNOS_URL=... python tests/test_bindings.py

Covers:
  - Control CRUD: bindings + hosts insert/list/delete, scoped to principal (A can't see/del B's).
  - API auth-scoping: a user token manages only its own bindings/hosts; 404 on another's id.
  - Resolver: repo-key > host-scoped > legacy file > env > default; offline reads cache only;
    no-git -> host_path/default; hostname sanitization is deterministic; resolve() makes no network call.
  - Migration: ns_overrides.json path-keys -> correct repo / host_path bindings.
"""
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import psycopg
from psycopg.rows import dict_row
from core.control import Control

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
URL = os.environ.get("MEMNOS_URL", "http://127.0.0.1:8900")
PASS = FAIL = 0


def call(method, path, token=None, body=None):
    req = urllib.request.Request(URL + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json",
                 **({"Authorization": "Bearer " + token} if token else {})})
    try:
        r = urllib.request.urlopen(req, timeout=10)
        return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += bool(cond); FAIL += (not cond)


# ---------------------------------------------------------------------------
def test_control_crud(conn, a_id, b_id):
    print("=== Control CRUD (principal-scoped) ===")
    # bindings for A
    Control.upsert_binding(conn, a_id, "repo", "github.com/x/a", "proj:a")
    row = Control.upsert_binding(conn, a_id, "host_path", "/tmp/proj", "proj:local", host_id="hostA")
    check("upsert returns row with id", isinstance(row.get("id"), int))
    # update-on-conflict (same key -> namespace changes, no new row)
    Control.upsert_binding(conn, a_id, "repo", "github.com/x/a", "proj:a2")
    a_binds = Control.list_bindings(conn, a_id)
    check("A has 2 bindings (upsert updated, didn't duplicate)", len(a_binds) == 2)
    check("upsert changed namespace", any(b["namespace"] == "proj:a2" for b in a_binds))

    # bindings for B (isolation)
    Control.upsert_binding(conn, b_id, "repo", "github.com/x/b", "proj:b")
    b_binds = Control.list_bindings(conn, b_id)
    check("B sees only its own binding", len(b_binds) == 1 and b_binds[0]["namespace"] == "proj:b")
    check("A's list excludes B's binding", all(b["namespace"] != "proj:b" for b in a_binds))

    # delete is principal-scoped: B cannot delete A's binding id
    a_bid = a_binds[0]["id"]
    check("B cannot delete A's binding (scoped delete returns False)",
          Control.delete_binding(conn, b_id, a_bid) is False)
    check("A's binding still present after B's attempt", len(Control.list_bindings(conn, a_id)) == 2)
    check("A deletes its own binding", Control.delete_binding(conn, a_id, a_bid) is True)
    check("A now has 1 binding", len(Control.list_bindings(conn, a_id)) == 1)

    # hosts
    Control.upsert_host(conn, a_id, "hostA", "Work Laptop")
    Control.upsert_host(conn, a_id, "hostA")                       # check-in keeps name
    hosts = Control.list_hosts(conn, a_id)
    check("host registered once, name preserved on check-in",
          len(hosts) == 1 and hosts[0]["friendly_name"] == "Work Laptop")
    Control.upsert_host(conn, a_id, "hostA", "Renamed")           # rename
    check("host rename applied", Control.list_hosts(conn, a_id)[0]["friendly_name"] == "Renamed")
    Control.upsert_host(conn, b_id, "hostB")
    check("B sees only its own host", len(Control.list_hosts(conn, b_id)) == 1)

    # resolve_binding helper (server-side): repo beats host_repo beats host_path
    Control.upsert_binding(conn, a_id, "repo", "github.com/x/r", "ns:repo")
    Control.upsert_binding(conn, a_id, "host_repo", "github.com/x/r", "ns:hostrepo", host_id="hostA")
    r = Control.resolve_binding(conn, a_id, repo_key="github.com/x/r", host_id="hostA")
    check("resolve_binding prefers repo (host-agnostic)", r and r["namespace"] == "ns:repo")


def test_api_scoping(conn, a_id, b_id, a_tok, b_tok):
    print("=== API auth-scoping (own-principal isolation) ===")
    check("no token -> 401", call("GET", "/bindings")[0] == 401)
    # A creates a binding via API
    s, j = call("POST", "/bindings", a_tok,
                {"key_type": "repo", "key": "github.com/api/a", "namespace": "proj:api-a"})
    check("A POST /bindings 200", s == 200 and j["binding"]["namespace"] == "proj:api-a")
    a_bid = j["binding"]["id"]
    # B's listing must NOT contain A's binding
    s, jb = call("GET", "/bindings", b_tok)
    check("B GET /bindings excludes A's binding",
          s == 200 and all(b["id"] != a_bid for b in jb.get("bindings", [])))
    # B cannot delete A's binding id -> 404 (not 200, not 500)
    check("B DELETE A's binding id -> 404", call("DELETE", f"/bindings/{a_bid}", b_tok)[0] == 404)
    # A can list + delete its own
    s, ja = call("GET", "/bindings", a_tok)
    check("A GET /bindings includes its binding", any(b["id"] == a_bid for b in ja["bindings"]))
    check("A DELETE own binding -> 200", call("DELETE", f"/bindings/{a_bid}", a_tok)[0] == 200)
    check("deleted binding -> 404 on re-delete", call("DELETE", f"/bindings/{a_bid}", a_tok)[0] == 404)
    # validation
    check("bad key_type -> 400",
          call("POST", "/bindings", a_tok, {"key_type": "nope", "key": "k", "namespace": "n"})[0] == 400)
    check("host-scoped without host_id -> 400",
          call("POST", "/bindings", a_tok, {"key_type": "host_repo", "key": "k", "namespace": "n"})[0] == 400)
    # hosts API scoping
    s, _ = call("POST", "/hosts", a_tok, {"machine_id": "api-hostA", "friendly_name": "A box"})
    check("A POST /hosts 200", s == 200)
    s, jh = call("GET", "/hosts", b_tok)
    check("B GET /hosts excludes A's host", all(h["machine_id"] != "api-hostA" for h in jh.get("hosts", [])))


def test_resolver():
    print("=== resolver (cache-only, never network) ===")
    import nsresolve
    # hostname sanitization is deterministic + idempotent
    m1, m2 = nsresolve.machine_id(), nsresolve.machine_id()
    check("machine_id deterministic", m1 == m2 and m1 and "-" not in (m1[0] + m1[-1]))
    # remote normalization
    check("remote normalize ssh == https",
          nsresolve._normalize_remote("git@github.com:thameema/memnos.git")
          == nsresolve._normalize_remote("https://github.com/thameema/memnos")
          == "github.com/thameema/memnos")

    # redirect resolver's cache/override/dirs to a scratch dir so we touch no real files
    tmp = tempfile.mkdtemp(prefix="nsres_")
    nsresolve._DIR = tmp
    nsresolve._CACHE = os.path.join(tmp, "bindings_cache.json")
    nsresolve._OVR = os.path.join(tmp, "ns_overrides.json")
    nsresolve._MID = os.path.join(tmp, "machine_id")
    mid = nsresolve.machine_id()

    # a non-git scratch cwd so repo_key() is None and _git_root is None
    work = tempfile.mkdtemp(prefix="nswork_")
    rkey = "github.com/test/precedence"

    def write_cache(binds):
        json.dump({"bindings": binds, "fetched_at": 0}, open(nsresolve._CACHE, "w"))

    # monkeypatch repo_key to simulate a repo at this cwd (no real git needed)
    orig_repo_key = nsresolve.repo_key
    nsresolve.repo_key = lambda cwd=None: rkey

    os.environ.pop("MEMNOS_NS", None)

    # precedence 1: explicit arg wins over everything
    write_cache([{"key_type": "repo", "key": rkey, "namespace": "ns:repo"}])
    check("explicit arg beats cache",
          nsresolve.resolve({"cwd": work, "namespace": "ns:explicit"}) == "ns:explicit")

    # precedence 2: cache repo match beats host-scoped + legacy + env + default
    write_cache([
        {"key_type": "host_repo", "key": rkey, "host_id": mid, "namespace": "ns:hostrepo"},
        {"key_type": "repo", "key": rkey, "namespace": "ns:repo"},
    ])
    json.dump({work: "ns:legacy"}, open(nsresolve._OVR, "w"))
    os.environ["MEMNOS_NS"] = "ns:env"
    check("cache repo-key match wins", nsresolve.resolve({"cwd": work}) == "ns:repo")

    # precedence 3: host-scoped (host_repo) beats legacy + env when no repo binding
    write_cache([{"key_type": "host_repo", "key": rkey, "host_id": mid, "namespace": "ns:hostrepo"}])
    check("host_repo (this host) beats legacy/env", nsresolve.resolve({"cwd": work}) == "ns:hostrepo")

    # host-scoped binding for a DIFFERENT host must NOT match
    write_cache([{"key_type": "host_repo", "key": rkey, "host_id": "other-host", "namespace": "ns:wrong"}])
    check("host_repo on another host does NOT match (falls to legacy)",
          nsresolve.resolve({"cwd": work}) == "ns:legacy")

    # host_path match (no repo) on this host
    nsresolve.repo_key = lambda cwd=None: None
    write_cache([{"key_type": "host_path", "key": work, "host_id": mid, "namespace": "ns:hostpath"}])
    check("host_path (this host + abspath) matches", nsresolve.resolve({"cwd": work}) == "ns:hostpath")

    # precedence 4: legacy file when cache empty
    write_cache([])
    check("legacy ns_overrides.json used when cache empty",
          nsresolve.resolve({"cwd": work}) == "ns:legacy")

    # precedence 5: env when no cache + no legacy
    os.remove(nsresolve._OVR)
    check("env default when no binding/legacy", nsresolve.resolve({"cwd": work}) == "ns:env")

    # precedence 6: derived default when nothing set (no git -> cwd basename)
    os.environ.pop("MEMNOS_NS", None)
    check("derived proj:<cwd-basename> default",
          nsresolve.resolve({"cwd": work}) == "proj:" + os.path.basename(work))

    # offline: no cache file at all -> still resolves (default), no exception/network
    os.remove(nsresolve._CACHE)
    check("no cache file -> graceful default", nsresolve.resolve({"cwd": work}).startswith("proj:"))

    # refresh with no URL/token is a quiet no-op (best-effort), never raises
    os.environ.pop("MEMNOS_URL", None); os.environ.pop("MEMNOS_TOKEN", None)
    check("refresh() best-effort no-op without creds", nsresolve.refresh() is False)

    nsresolve.repo_key = orig_repo_key


def test_migration(conn, a_id, a_tok):
    print("=== migration (ns_overrides.json -> server bindings) ===")
    import nsresolve
    tmp = tempfile.mkdtemp(prefix="nsmig_")
    nsresolve._DIR = tmp
    nsresolve._OVR = os.path.join(tmp, "ns_overrides.json")
    nsresolve._MID = os.path.join(tmp, "machine_id")
    # one git-repo path (a real repo: this checkout), one non-repo path
    repo_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    nogit = tempfile.mkdtemp(prefix="nsmig_nogit_")
    json.dump({repo_path: "proj:fromrepo", nogit: "proj:frompath"},
              open(nsresolve._OVR, "w"))

    # drive the CLI migrate logic directly (it POSTs to the server)
    import memnos_cli
    cfg = {"url": URL}

    class A:  # noqa
        token = a_tok
    memnos_cli.cmd_bindings_migrate(A(), cfg)

    binds = {(b["key_type"], b["namespace"]) for b in Control.list_bindings(conn, a_id)}
    rkey = nsresolve.repo_key(repo_path)
    if rkey:
        check("repo path -> 'repo' binding", ("repo", "proj:fromrepo") in binds)
    else:
        check("repo path (no remote) -> host_path binding", ("host_path", "proj:fromrepo") in binds)
    check("non-repo path -> host_path binding", ("host_path", "proj:frompath") in binds)


def main():
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)
    a_id = Control.create_principal(conn, "test-bind-a", "agent")
    b_id = Control.create_principal(conn, "test-bind-b", "agent")
    a_tok = Control.mint_token(conn, a_id, "test")
    b_tok = Control.mint_token(conn, b_id, "test")

    try:
        test_control_crud(conn, a_id, b_id)
        test_api_scoping(conn, a_id, b_id, a_tok, b_tok)
        test_resolver()
        test_migration(conn, a_id, a_tok)
    finally:
        with conn.cursor() as c:
            for pid in (a_id, b_id):
                c.execute("DELETE FROM memnos_control.bindings WHERE principal_id=%s", (pid,))
                c.execute("DELETE FROM memnos_control.hosts WHERE principal_id=%s", (pid,))
                c.execute("DELETE FROM memnos_control.api_tokens WHERE principal_id=%s", (pid,))
            c.execute("DELETE FROM memnos_control.principals WHERE name IN ('test-bind-a','test-bind-b')")

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
