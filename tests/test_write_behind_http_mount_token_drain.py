"""issue #45 — the actual bug: offline_queue.drain() used to drain EVERY item with one
shared `token` argument. That's correct for memnos_mcp.py's stdio adapter (a real
per-process MEMNOS_TOKEN), but under the streamable-HTTP MCP mount (memnos_server.py,
issue #37 Layer 1) the mount never sets MEMNOS_TOKEN for itself — memnos_mcp.TOKEN is
permanently "" there. Before the fix, `_drain_offline_queue()`'s
`offline_queue.drain(cfg, URL, TOKEN, ...)` therefore drained every HTTP-mount-queued
item with an empty Authorization header -> 401 -> offline_queue.is_transient() correctly
classifies 401 as PERMANENT (an auth problem a retry can never fix) -> the item is moved
to `.rejected` instead of replayed. A write that was queued during a genuine transient
outage silently vanishes instead of syncing, defeating PR #40's durability guarantee
specifically for HTTP-transport callers.

The fix: enqueue() now optionally captures the caller's own token alongside the item;
drain() uses THAT item's own token when present, falling back to its `fallback_token`
arg only for items that don't have one (pre-#45 queue files, or the stdio adapter's
normal case where the captured token and the fallback are the same value anyway).

This test seeds the queue directly (bypassing memnos_mcp.py — tests/test_mcp_http_
write_behind_seam.py already proves memnos_mcp.py's remember()/memory_write() capture
the caller's real per-request token over the actual streamable-HTTP wire) with the
exact three shapes that matter:
  - an item tagged with tenant A's own token
  - an item tagged with tenant B's own token (different tenant, different token,
    different namespace, sharing the SAME queue directory and the SAME drain() call --
    the multi-tenant scenario a single shared TOKEN can never support)
  - an item with NO captured token at all -- the exact on-disk shape every HTTP-mount
    queued item had before this fix (and what any queue file written by a pre-#45
    memnos_mcp.py still looks like after an upgrade -- not recoverable, fix-forward only)
then drains the whole queue with fallback_token="" -- exactly what
memnos_mcp.TOKEN resolves to under the HTTP mount -- against a REAL memnos_server.py
subprocess (not a stub), so the reproduction is the real 401-misclassification path
end to end, and the fix is proven against a real store, not a mocked assertion.

Run: python tests/test_write_behind_http_mount_token_drain.py
(spawns its own server + throwaway database; does not require one already running)
"""
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import offline_queue

PY = sys.executable
BASE_DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
# Dedicated throwaway DB, isolated HOME on the server -> no OPENAI_API_KEY -> local-384
# embeddings, same reasoning as tests/test_write_behind_kill_restart.py.
TEST_DB = "memnos_test_wb_http_mount_token_drain"
SCHEMA = "tenant_memnos"
NS_A = "test:wb-http-mount-token-a"
NS_B = "test:wb-http-mount-token-b"
TEXT_A = "wb-http-mount-token TENANT-A FACT: the ice sculpture contest moved to Harbin."
TEXT_B = "wb-http-mount-token TENANT-B FACT: the kite festival relocated to Weifang."
TEXT_TOKENLESS = "wb-http-mount-token TOKENLESS FACT: the tulip parade moved to Keukenhof."
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
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_up(url, tries=150, interval=2):
    import urllib.request
    # issue #59: /readyz, not /healthz — this is a "wait until the server can serve
    # real traffic" gate; /healthz's 200 (liveness only) gives no guarantee the
    # pool/HNSW indexes are actually warm. /readyz does.
    for _ in range(tries):
        try:
            urllib.request.urlopen(url + "/readyz", timeout=2)
            return True
        except Exception:
            time.sleep(interval)
    return False


def start_server(server_home, dsn, port):
    env = dict(os.environ, HOME=server_home, MEMNOS_DSN=dsn, MEMNOS_PORT=str(port))
    env.pop("OPENAI_API_KEY", None)   # belt & suspenders — temp HOME has no config.json anyway
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
    pid_a = Control.create_principal(conn, "wb-http-mount-token-a", "agent")
    Control.grant(conn, pid_a, NS_A)
    tok_a = Control.mint_token(conn, pid_a, "wb-http-mount-token-a")
    pid_b = Control.create_principal(conn, "wb-http-mount-token-b", "agent")
    Control.grant(conn, pid_b, NS_B)
    tok_b = Control.mint_token(conn, pid_b, "wb-http-mount-token-b")

    port = free_port()
    url = f"http://127.0.0.1:{port}"
    server_home = tempfile.mkdtemp(prefix="memnos_wb_httoken_srv_")
    client_home = tempfile.mkdtemp(prefix="memnos_wb_httoken_client_")
    cfg_dir = os.path.join(client_home, ".memnos")
    proc = logf = None

    try:
        proc, logf, logpath = start_server(server_home, DSN, port)
        up = wait_up(url)
        check("test server came up (isolated HOME -> local-384 mode, deterministic)", up)
        if not up:
            print("---- server log tail ----")
            try:
                print(open(logpath).read()[-2000:])
            except Exception:
                pass
            print(f"\n{PASS} passed, {FAIL} failed")
            sys.exit(1)

        # Seed the queue directly — reproduces exactly what memnos_mcp.py's remember()/
        # memory_write() produce on a transient failure, for all three shapes that matter
        # (tests/test_mcp_http_write_behind_seam.py separately proves the real HTTP MCP
        # wire path captures a per-request token into the on-disk item the same way).
        offline_queue.enqueue(cfg_dir, NS_A, TEXT_A, "user", token=tok_a)
        offline_queue.enqueue(cfg_dir, NS_B, TEXT_B, "user", token=tok_b)
        # No token= at all: the exact shape every item had before this fix, under the
        # HTTP mount (memnos_mcp.TOKEN there is permanently "") -- and what any queue
        # file already on disk from a pre-#45 memnos_mcp.py still looks like.
        offline_queue.enqueue(cfg_dir, NS_A, TEXT_TOKENLESS, "user")

        qdir = offline_queue.queue_dir(cfg_dir)
        seeded = sorted(os.listdir(qdir))
        check("exactly three items seeded into the shared queue dir", len(seeded) == 3, str(seeded))

        # fallback_token="" -- exactly what memnos_mcp.TOKEN resolves to under the HTTP
        # mount (MEMNOS_TOKEN is never set for the server's own process). Before the fix
        # this was the ONLY token drain() ever used, for every item.
        drained, rejected = offline_queue.drain(cfg_dir, url, "", timeout=15)

        check("tenant A's and tenant B's items BOTH drained successfully using their OWN "
              "captured tokens (not the empty HTTP-mount fallback) -- this is the fix",
              drained == 2, f"drained={drained}")
        check("the tokenless item (pre-#45 on-disk shape) is the ONLY one rejected -- "
              "an item with no captured token and no usable fallback correctly still "
              "401s, exactly as any HTTP-mount item did before this fix, but WITHOUT "
              "blocking the two tokened items behind it",
              rejected == 1, f"rejected={rejected}")

        remaining = sorted(os.listdir(qdir)) if os.path.isdir(qdir) else []
        check("exactly one artifact remains in the queue dir after drain, and it's the "
              "tokenless item set aside as .rejected (not silently dropped, not retried "
              "forever, not blocking A/B)",
              len(remaining) == 1 and remaining[0].endswith(".rejected"),
              str(remaining))

        row_a = conn.execute(f"SELECT count(*) AS n FROM {SCHEMA}.raw_turns "
                             f"WHERE namespace=%s AND text=%s", (NS_A, TEXT_A)).fetchone()
        check("tenant A's write actually landed in the store, under tenant A's OWN "
              "namespace", row_a["n"] == 1, f"n={row_a['n']}")

        row_b = conn.execute(f"SELECT count(*) AS n FROM {SCHEMA}.raw_turns "
                             f"WHERE namespace=%s AND text=%s", (NS_B, TEXT_B)).fetchone()
        check("tenant B's write likewise landed, under tenant B's OWN namespace -- proves "
              "per-item token resolution, not a shared/reused credential",
              row_b["n"] == 1, f"n={row_b['n']}")

        row_cross = conn.execute(f"SELECT count(*) AS n FROM {SCHEMA}.raw_turns "
                                 f"WHERE namespace=%s AND text=%s", (NS_B, TEXT_A)).fetchone()
        check("tenant A's text never landed under tenant B's namespace (no cross-tenant "
              "bleed)", row_cross["n"] == 0, f"n={row_cross['n']}")

        row_tokenless = conn.execute(f"SELECT count(*) AS n FROM {SCHEMA}.raw_turns "
                                     f"WHERE text=%s", (TEXT_TOKENLESS,)).fetchone()
        check("the tokenless item was never replayed into the store (permanently "
              "rejected means rejected, not silently retried and eventually smuggled in)",
              row_tokenless["n"] == 0, f"n={row_tokenless['n']}")
    finally:
        stop(proc, logf)
        conn.close()
        shutil.rmtree(server_home, ignore_errors=True)
        shutil.rmtree(client_home, ignore_errors=True)
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
