"""Integration seam: issue #37 Layer 1 (streamable-HTTP MCP transport, PR #38) x Layer 3
(durable write-behind queue, PR #40) — a combination that has never run together before
this merge and that neither PR's own test suite exercises.

Before this merge: PR #38's memnos_mcp.py had no offline_queue calls at all (write-behind
didn't exist yet on that branch). PR #40's offline_queue wiring landed on master, but
master never had the HTTP mount — so every existing tests/test_write_behind_*.py test
exercises the queue via a bare Python function call (`memnos_mcp.remember.fn(...)`,
module-level TOKEN/URL/MEMNOS_NS env config) or via memnos_cli.py's hooks — i.e. always
the single-tenant STDIO-shaped code path, never through _REQUEST_CTX.

Under the HTTP mount, the SAME memnos_mcp.py functions are shared by MANY different
callers on ONE process (see memnos_mcp.py's _ns_source(): "HTTP mount: if _REQUEST_CTX
is set ... its namespace ALWAYS wins"). The conflict-resolution merging PR #38 into
master's write-behind changes had to keep that HTTP-mount `_ns()` resolution intact
INSIDE the newly-merged offline_queue exception handling (memnos_mcp.py's recall(),
403 branch) rather than let it regress to the stdio-only module-level NS global. This
test locks in the load-bearing half of that: that a queued write's on-disk `namespace`
tag, produced by a REAL streamable-HTTP MCP client call (not a bare function call), is
resolved per-request — not from any process-wide default — even when two different
tenants share the one mount.

Mechanism for a deterministic, portable (no docker/network-outage tricks) transient
failure: the dedicated server subprocess is started with an OPENAI_API_KEY that is
present but garbage. _build_embedder() only checks *presence* to switch into OpenAI
1536-d mode (memnos_server.py's own contract — the real key is validated by OpenAI
itself, not memnos), so every /remember's EMBED() call deterministically 401s against
the real OpenAI API, which the server's own request handler turns into an uncaught-500.
httpx.HTTPStatusError(500) -> offline_queue.is_transient() == True -> enqueued, exactly
the same failure shape the docstring in offline_queue.is_transient() describes as
"an embed-time error inside /remember". No server process is killed and nothing is
asserted about draining/replay — this test is scoped to the enqueue-time namespace
tagging only; draining under the HTTP mount is a separate, NOT currently exercised,
concern (see the merge report).

Run: python tests/test_mcp_http_write_behind_seam.py
(spawns its own server + throwaway database; does not require one already running)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import anyio
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

PY = sys.executable
BASE_DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
# Dedicated throwaway DB: the server here runs in OpenAI 1536-d mode (garbage key, see
# module docstring), which needs its own 1536-d schema — never the shared 384-d db every
# local-mode test uses.
TEST_DB = "memnos_test_mcp_http_write_behind"
SCHEMA = "tenant_memnos"
NS_A = "test:mcp-http-wb-seam-a"
NS_B = "test:mcp-http-wb-seam-b"
TEXT_A = "wb-seam TENANT-A FACT: the aurora borealis festival moved to Tromso."
TEXT_B = "wb-seam TENANT-B FACT: the lantern parade relocated to Kyoto."
PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if (detail and not cond) else ""))
    PASS += bool(cond); FAIL += (not cond)


def with_dbname(dsn, dbname):
    base, _, _ = dsn.rpartition("/")
    return f"{base}/{dbname}"


def redacted(dsn):
    u = urlsplit(dsn)
    netloc = u.netloc
    if "@" in netloc:
        userinfo, host = netloc.rsplit("@", 1)
        user = userinfo.split(":", 1)[0]
        netloc = f"{user}:***@{host}"
    return u._replace(netloc=netloc).geturl()


def admin_dsn_candidates(dsn):
    u = urlsplit(dsn)
    host, port = (u.hostname or "localhost"), (u.port or 5432)
    base_admin = dsn.rsplit("/", 1)[0] + "/postgres"
    os_user_admin = f"postgresql://{os.environ.get('USER', 'postgres')}@{host}:{port}/postgres"
    return [base_admin, os_user_admin]


def free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_up(url, tries=90, interval=0.5):
    for _ in range(tries):
        try:
            r = httpx.get(url + "/healthz", timeout=2)
            if r.status_code == 200:
                return True
        except httpx.TransportError:
            pass
        time.sleep(interval)
    return False


def start_server(server_home, dsn, port):
    env = dict(os.environ, HOME=server_home, MEMNOS_DSN=dsn, MEMNOS_PORT=str(port),
               MEMNOS_SECRET_KEY="d2Jfc2VhbV90ZXN0X2tleV8zMmJfZXhhY3RseV9vaw==")
    # present but invalid — switches _build_embedder() into OpenAI 1536-d mode so every
    # /remember's EMBED() call deterministically 401s (see module docstring)
    env["OPENAI_API_KEY"] = "sk-deliberately-invalid-for-write-behind-seam-test"
    logpath = os.path.join(server_home, "server.log")
    logf = open(logpath, "w")
    proc = subprocess.Popen([PY, os.path.join(ROOT, "memnos_server.py")], cwd=ROOT, env=env,
                            stdout=logf, stderr=subprocess.STDOUT)
    return proc, logf, logpath


def stop(proc, logf=None):
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
            try:
                proc.wait(timeout=10)
            except Exception:
                pass
    if logf:
        try:
            logf.close()
        except Exception:
            pass


async def _remember_over_http_mcp(mcp_url, token, ns, text):
    async with streamablehttp_client(
        mcp_url, headers={"Authorization": f"Bearer {token}", "X-Memnos-Namespace": ns},
    ) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("remember", {"text": text})
            return result.content[0].text


def main():
    import psycopg
    from psycopg.rows import dict_row
    from core.control import Control

    owner = urlsplit(BASE_DSN).username or "memnos"
    su_dsn = None
    errors = []
    for candidate in admin_dsn_candidates(BASE_DSN):
        try:
            maint = psycopg.connect(candidate, autocommit=True)
            maint.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')
            maint.execute(f'CREATE DATABASE "{TEST_DB}" OWNER {owner}')
            maint.close()
            maint_db = psycopg.connect(with_dbname(candidate, TEST_DB), autocommit=True)
            maint_db.execute("CREATE EXTENSION IF NOT EXISTS vector")
            maint_db.close()
            su_dsn = candidate
            break
        except Exception as e:
            errors.append(f"{redacted(candidate)}: {e}")
    if su_dsn is None:
        raise RuntimeError("could not bootstrap the test database via any admin DSN "
                            "candidate:\n  " + "\n  ".join(errors))
    DSN = with_dbname(BASE_DSN, TEST_DB)

    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)
    tok_a = tok_b = None
    for name, ns in (("wb-seam-tenant-a", NS_A), ("wb-seam-tenant-b", NS_B)):
        pid = Control.create_principal(conn, name, "agent")
        Control.grant(conn, pid, ns)
        tok = Control.mint_token(conn, pid, "wb-seam-test")
        if ns == NS_A:
            tok_a = tok
        else:
            tok_b = tok

    port = free_port()
    url = f"http://127.0.0.1:{port}"
    mcp_url = url + "/mcp"
    server_home = tempfile.mkdtemp(prefix="memnos_wb_http_seam_")
    proc = logf = None

    try:
        proc, logf, logpath = start_server(server_home, DSN, port)
        up = wait_up(url, tries=150, interval=2)
        check("dedicated server (OpenAI 1536-d mode) came up", up)
        if not up:
            print("---- server log tail ----")
            try:
                print(open(logpath).read()[-2000:])
            except Exception:
                pass
            print(f"\n{PASS} passed, {FAIL} failed")
            sys.exit(1)

        try:
            text_a_result = anyio.run(_remember_over_http_mcp, mcp_url, tok_a, NS_A, TEXT_A)
        except Exception as e:
            check("tenant A's remember() over the real HTTP MCP transport did not raise",
                  False, f"{type(e).__name__}: {e}")
            text_a_result = ""
        else:
            check("tenant A's remember() over the real HTTP MCP transport did not raise", True)

        try:
            text_b_result = anyio.run(_remember_over_http_mcp, mcp_url, tok_b, NS_B, TEXT_B)
        except Exception as e:
            check("tenant B's remember() over the real HTTP MCP transport did not raise",
                  False, f"{type(e).__name__}: {e}")
            text_b_result = ""
        else:
            check("tenant B's remember() over the real HTTP MCP transport did not raise", True)

        check("tenant A's write, hitting a deterministic embed-time 500, comes back QUEUED "
              "(not a raised ToolError) — proves is_transient(500) classification survives "
              "the real wire path", "queued" in text_a_result.lower(), text_a_result)
        check("tenant B's write likewise comes back QUEUED",
              "queued" in text_b_result.lower(), text_b_result)

        qdir = os.path.join(server_home, ".memnos", "offline_queue")
        qfiles = sorted(f for f in os.listdir(qdir)) if os.path.isdir(qdir) else []
        check("exactly two items landed in the (single, shared-by-both-tenants) queue dir "
              "under the mount's own HOME", len(qfiles) == 2, str(qfiles))

        items = []
        for f in qfiles:
            try:
                with open(os.path.join(qdir, f)) as fh:
                    items.append(json.load(fh))
            except Exception:
                items.append({})

        by_text = {it.get("text"): it for it in items}
        item_a = by_text.get(TEXT_A, {})
        item_b = by_text.get(TEXT_B, {})
        check("both distinctive texts are present in the queue (nothing dropped/merged)",
              TEXT_A in by_text and TEXT_B in by_text, str(list(by_text)))
        check("tenant A's queued item is tagged with tenant A's OWN per-request namespace "
              "(from _REQUEST_CTX via the X-Memnos-Namespace header on ITS session) — "
              "not tenant B's, and not any process-wide default",
              item_a.get("namespace") == NS_A,
              f"got {item_a.get('namespace')!r}")
        check("tenant B's queued item is likewise tagged with tenant B's OWN namespace, "
              "never tenant A's — proves per-request resolution, not a shared/stale global",
              item_b.get("namespace") == NS_B,
              f"got {item_b.get('namespace')!r}")
    finally:
        stop(proc, logf)
        conn.close()
        shutil.rmtree(server_home, ignore_errors=True)
        try:
            maint2 = psycopg.connect(su_dsn, autocommit=True)
            maint2.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')
            maint2.close()
        except Exception as e:
            print(f"(cleanup warning: could not drop {TEST_DB}: {e})")

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
