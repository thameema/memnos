"""issue #42 — replayed write-behind writes must be stamped with the REPLAY-commit time
on the bi-temporal OBSERVATION axis (`observed_at` / semantic.observed_at, "known_at"),
never with the original enqueue time and never with a value the client supplies. The
EVENT-time axis (`valid_from`, "valid_at") is a separate, orthogonal knob that MAY be
anchored to the original enqueue time (`queued_at`, sent by offline_queue.py's
`_post_remember`) so a long outage doesn't shift a relative date ("yesterday") in the
queued text to mean the day before it replayed instead of the day before it was said.

Design (memnos_server.py's `_replay_valid_anchor` / `_remember_phased`, core/service.py's
`_write_fact` `valid_anchor` param): `observed_at` is ALWAYS this server's own clock at
receive/commit time, on every path, replay included — never read from the request body.
`queued_at` is the ONLY client-influenced timestamp the replay path accepts, and it only
ever feeds `parse_event_date`'s fallback anchor for THIS fact's `valid_from` — it can
never make a stale replayed write look "just learned" and win a supersession race against
a fact genuinely written by someone else while the queue was down.

Runs a REAL memnos_server.py subprocess (dedicated throwaway Postgres database, isolated
HOME so no real ~/.memnos/config.json / OpenAI key leaks in) with MEMNOS_FAKE_EXTRACT=1 so
write_facts persists actual `semantic` rows (deterministic regex-NER extractor, $0 — see
memnos_server.py's `_fake_extract`) through the SAME P2(extract)->P3(write_facts) ordering
the real LLM path uses, over HTTP — not a direct store-layer call.

SCENARIO 1 (regression, real time lapse): enqueue via the real MCP adapter while the
server is confirmed down, sleep ~2.5s, restart, replay. Asserts `known_at` (both
raw_turns.observed_at and semantic.observed_at) lands meaningfully AFTER the captured
`queued_at` and close to replay-time, while semantic.valid_from lands close to the
ORIGINAL `queued_at` — proving the two axes genuinely diverge across a real outage.

SCENARIO 2 (malicious client): a queue file is constructed directly (bypassing
offline_queue.enqueue(), which always computes `queued_at` from its own wall clock and
takes no caller-supplied value) with `queued_at` backdated to the Unix epoch AND extra
`observed_at`/`known_at` keys also backdated to the epoch, to prove the server ignores
ALL of these for the observation axis regardless of field name. Also fires one request
that bypasses offline_queue.py entirely — a raw HTTP POST straight to /remember with
`observed_at`/`known_at` in the body — so the invariant is proven server-side, not just
as a property of the (trusted) offline_queue.py client library.

Run: python tests/test_write_behind_replay_timestamp.py
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
PY = sys.executable
BASE_DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
# Dedicated, throwaway database — distinct from test_write_behind_kill_restart.py's own
# TEST_DB so the two files can never race each other's DROP/CREATE DATABASE.
TEST_DB = "memnos_test_write_behind_replay_ts"
SCHEMA = "tenant_memnos"
NS = "test:wb-replay-ts"
QUEUED_TEXT = "wb-replay-ts QUEUED FACT: Zorblatt relocated the archive to Vantaa."
QUEUED_ENTITY = "Zorblatt"
MALICIOUS_TEXT = "wb-replay-ts MALICIOUS FACT: Malbolge relocated the vault to Suomussalmi."
MALICIOUS_ENTITY = "Malbolge"
DIRECT_TEXT = "wb-replay-ts DIRECT-POST FACT: Quixotry relocated the depot to Rovaniemi."
DIRECT_ENTITY = "Quixotry"
OUTAGE_SLEEP_S = 2.5
PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
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
    """Same two admin-DSN strategies as test_write_behind_kill_restart.py — see that
    file's docstring for why both are needed (differs by environment which role has
    CREATE DATABASE / CREATE EXTENSION on this host:port)."""
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


def wait_up(url, tries=90, interval=0.5):
    for _ in range(tries):
        try:
            urllib.request.urlopen(url + "/healthz", timeout=2)
            return True
        except Exception:
            time.sleep(interval)
    return False


def wait_down(url, tries=40, interval=0.25):
    for _ in range(tries):
        try:
            urllib.request.urlopen(url + "/healthz", timeout=1)
            time.sleep(interval)
        except Exception:
            return True
    return False


def snapshot_tree(root):
    out = set()
    for dirpath, _, files in os.walk(root):
        for f in files:
            out.add(os.path.relpath(os.path.join(dirpath, f), root))
    return out


def start_server(server_home, dsn, port, extra_env=None):
    env = dict(os.environ, HOME=server_home, MEMNOS_DSN=dsn, MEMNOS_PORT=str(port))
    env.pop("OPENAI_API_KEY", None)   # isolated HOME has no config.json anyway — belt+suspenders
    env["MEMNOS_FAKE_EXTRACT"] = "1"  # $0 deterministic extractor -> real semantic rows over HTTP
    if extra_env:
        env.update(extra_env)
    logpath = os.path.join(server_home, "server.log")
    logf = open(logpath, "w")
    proc = subprocess.Popen([PY, os.path.join(ROOT, "memnos_server.py")], cwd=ROOT, env=env,
                            stdout=logf, stderr=subprocess.STDOUT)
    return proc, logf, logpath


def stop(proc, logf=None):
    if proc.poll() is None:
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
    import offline_queue

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
    pid = Control.create_principal(conn, "wb-replay-ts-agent", "agent")
    Control.grant(conn, pid, NS)
    token = Control.mint_token(conn, pid, "wb-replay-ts")

    port = free_port()
    url = f"http://127.0.0.1:{port}"
    server_home = tempfile.mkdtemp(prefix="memnos_wb_ts_srv_")
    client_home = tempfile.mkdtemp(prefix="memnos_wb_ts_client_")
    proc = logf = None

    try:
        proc, logf, logpath = start_server(server_home, DSN, port)
        up = wait_up(url, tries=150, interval=2)   # cold start budget — see kill_restart test
        check("test server came up (fake-extract, local-384, deterministic)", up)
        if not up:
            print("---- server log tail ----")
            try:
                print(open(logpath).read()[-2000:])
            except Exception:
                pass
            print(f"\n{PASS} passed, {FAIL} failed")
            sys.exit(1)

        os.environ["HOME"] = client_home
        os.environ["MEMNOS_URL"] = url
        os.environ["MEMNOS_TOKEN"] = token
        os.environ["MEMNOS_NS"] = NS
        import memnos_mcp
        remember = getattr(memnos_mcp.remember, "fn", memnos_mcp.remember)
        recall = getattr(memnos_mcp.recall, "fn", memnos_mcp.recall)

        # ================================================================
        # SCENARIO 1 — real outage, real time lapse, real replay
        # ================================================================
        print("=== SCENARIO 1: known_at = replay time, valid_at = original enqueue time ===")
        before = snapshot_tree(client_home)
        stop(proc, logf)
        check("server confirmed down before the queued write", wait_down(url))

        r1 = remember(QUEUED_TEXT)
        check("write against a dead server returns a QUEUED outcome",
              isinstance(r1, str) and "queued" in r1.lower())

        new_files = snapshot_tree(client_home) - before
        check("exactly one queue file was created", len(new_files) == 1)
        qpath = next(iter(new_files), "")
        with open(os.path.join(client_home, qpath)) as fh:
            qitem = json.load(fh)
        queued_at = qitem.get("queued_at")
        check("the queue file carries a queued_at timestamp",
              isinstance(queued_at, (int, float)) and queued_at > 0)

        time.sleep(OUTAGE_SLEEP_S)   # let REAL time pass while genuinely queued

        proc, logf, logpath = start_server(server_home, DSN, port)
        check("server came back up", wait_up(url))

        drained_ok = False
        for _ in range(30):
            recall("Zorblatt archive")   # opportunistic drain, same mechanism as recall()
            row = conn.execute(
                f"SELECT count(*) AS n FROM {SCHEMA}.raw_turns WHERE namespace=%s AND text=%s",
                (NS, QUEUED_TEXT)).fetchone()
            if row["n"] > 0:
                drained_ok = True
                break
            time.sleep(0.5)
        check("queued write was replayed into the store", drained_ok)
        check("the offline_queue file was removed after replay",
              not os.path.exists(os.path.join(client_home, qpath)))

        raw = conn.execute(
            f"SELECT observed_at FROM {SCHEMA}.raw_turns WHERE namespace=%s AND text=%s",
            (NS, QUEUED_TEXT)).fetchone()
        check("raw_turns row exists for the queued write", raw is not None)
        raw_observed = raw["observed_at"].timestamp() if raw else None

        # raw_turns lands synchronously (P1b) but extraction/write_facts for a replayed
        # (always async=True) item runs on a background _ingest_worker thread — poll
        # rather than assume it already landed the instant raw_turns did.
        sem = None
        for _ in range(20):
            sem = conn.execute(
                f"SELECT observed_at, valid_from FROM {SCHEMA}.semantic "
                f"WHERE namespace=%s AND statement=%s",
                (NS, f"{QUEUED_ENTITY} was mentioned.")).fetchone()
            if sem is not None:
                break
            time.sleep(0.3)
        replay_observed_ceiling = time.time()
        check("a semantic fact was extracted for the queued write (fake-extract NER)",
              sem is not None)

        if raw_observed is not None:
            check("raw_turns.observed_at (known_at) is MEANINGFULLY LATER than queued_at "
                  "(not the enqueue time)", raw_observed - queued_at >= 1.5)
            check("raw_turns.observed_at is close to actual replay-commit time "
                  "(loose bound — CI runners stall)",
                  abs(raw_observed - replay_observed_ceiling) < 120)

        if sem is not None:
            sem_observed = sem["observed_at"].timestamp()
            sem_valid = sem["valid_from"].timestamp()
            check("semantic.observed_at (known_at) is MEANINGFULLY LATER than queued_at",
                  sem_observed - queued_at >= 1.5)
            check("semantic.observed_at is close to actual replay-commit time",
                  abs(sem_observed - replay_observed_ceiling) < 120)
            check("semantic.valid_from (valid_at) equals the ORIGINAL queued_at "
                  "(within float/serialization tolerance), NOT replay time",
                  abs(sem_valid - queued_at) < 1.0)
            check("known_at and valid_at genuinely DIVERGED across the outage "
                  "(the actual bug this test regresses)",
                  sem_observed - sem_valid >= 1.5)

        # ================================================================
        # SCENARIO 2 — malicious client: forged queue file, backdated known_at
        # ================================================================
        print("=== SCENARIO 2: a forged queued_at/observed_at/known_at is ignored for known_at ===")
        malicious_home = tempfile.mkdtemp(prefix="memnos_wb_ts_malicious_")
        qdir = offline_queue.queue_dir(malicious_home)
        os.makedirs(qdir, exist_ok=True)
        # offline_queue.enqueue() always computes queued_at from its OWN wall clock and
        # takes no caller-supplied value — the only way to simulate a backdated queued_at
        # is to write the queue file directly, bypassing enqueue() entirely (tampering
        # with the local queue directory, not a normal client call).
        forged_item = {
            "namespace": NS, "text": MALICIOUS_TEXT, "speaker": "user", "async": True,
            "queued_at": 0.0,                          # 1970-01-01 — legitimate anchor knob
            "observed_at": 0.0, "known_at": 0.0,        # not real fields; must be inert either way
            "token": token,
        }
        fname = f"1_user_{uuid.uuid4().hex[:8]}.json"
        with open(os.path.join(qdir, fname), "w") as fh:
            json.dump(forged_item, fh)

        pre_drain2 = time.time()
        drained2, rejected2 = offline_queue.drain(malicious_home, url, token, timeout=10)
        check("the forged queue item drained without being rejected",
              drained2 == 1 and rejected2 == 0)

        raw2 = conn.execute(
            f"SELECT observed_at FROM {SCHEMA}.raw_turns WHERE namespace=%s AND text=%s",
            (NS, MALICIOUS_TEXT)).fetchone()
        check("raw_turns row exists for the malicious replayed write", raw2 is not None)
        sem2 = None
        for _ in range(20):
            sem2 = conn.execute(
                f"SELECT observed_at, valid_from FROM {SCHEMA}.semantic "
                f"WHERE namespace=%s AND statement=%s",
                (NS, f"{MALICIOUS_ENTITY} was mentioned.")).fetchone()
            if sem2 is not None:
                break
            time.sleep(0.3)   # async ingest worker — give P2/P3 a moment to land
        check("a semantic fact was extracted for the malicious write", sem2 is not None)

        if raw2 is not None:
            raw2_observed = raw2["observed_at"].timestamp()
            check("raw_turns.observed_at (known_at) is close to NOW, NOT the forged epoch-0 "
                  "value the malicious payload supplied",
                  abs(raw2_observed - pre_drain2) < 120 and raw2_observed > time.time() - 3600)
        if sem2 is not None:
            sem2_observed = sem2["observed_at"].timestamp()
            sem2_valid = sem2["valid_from"].timestamp()
            check("semantic.observed_at (known_at) is close to NOW, NOT the forged epoch-0 "
                  "value — the backdating attempt is ignored/overridden, per issue #42's "
                  "acceptance criterion",
                  abs(sem2_observed - pre_drain2) < 120 and sem2_observed > time.time() - 3600)
            # Documented, BOUNDED, and INTENTIONAL per the design decision: valid_from
            # (event time) is orthogonal to which fact wins a supersession race, so the
            # forged queued_at IS honored as this fact's own event-date anchor — the same
            # bounded effect any client already has today by putting an explicit backdated
            # date directly in a normal write's text. This is the one axis the design
            # decision explicitly does NOT lock down; asserting it here pins the boundary.
            check("semantic.valid_from legitimately follows the forged queued_at anchor "
                  "(event-time axis — orthogonal, NOT the supersession-deciding axis)",
                  abs(sem2_valid - 0.0) < 1.0)

        shutil.rmtree(malicious_home, ignore_errors=True)

        # ================================================================
        # SCENARIO 3 — direct HTTP POST bypassing offline_queue.py entirely
        # ================================================================
        print("=== SCENARIO 3: server ignores observed_at/known_at in the request body "
              "regardless of client (not just offline_queue.py's own discipline) ===")
        pre_direct = time.time()
        body = {"namespace": NS, "text": DIRECT_TEXT, "speaker": "user", "async": False,
                "observed_at": 0.0, "known_at": 0.0, "queued_at": 0.0}
        req = urllib.request.Request(
            f"{url}/remember", method="POST", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
        resp = urllib.request.urlopen(req, timeout=20)
        check("direct /remember POST with forged timestamp fields succeeds (never a 4xx/5xx)",
              resp.status == 200)

        raw3 = conn.execute(
            f"SELECT observed_at FROM {SCHEMA}.raw_turns WHERE namespace=%s AND text=%s",
            (NS, DIRECT_TEXT)).fetchone()
        check("raw_turns row exists for the direct-POST write", raw3 is not None)
        if raw3 is not None:
            raw3_observed = raw3["observed_at"].timestamp()
            check("direct POST: raw_turns.observed_at is close to NOW, not the forged "
                  "epoch-0 body field (server-side invariant, independent of any client "
                  "library's own discipline)",
                  abs(raw3_observed - pre_direct) < 120 and raw3_observed > time.time() - 3600)
        sem3 = conn.execute(
            f"SELECT observed_at, valid_from FROM {SCHEMA}.semantic "
            f"WHERE namespace=%s AND statement=%s",
            (NS, f"{DIRECT_ENTITY} was mentioned.")).fetchone()
        check("a semantic fact was extracted for the direct-POST write", sem3 is not None)
        if sem3 is not None:
            sem3_observed = sem3["observed_at"].timestamp()
            check("direct POST: semantic.observed_at is close to NOW, not the forged "
                  "epoch-0 body field",
                  abs(sem3_observed - pre_direct) < 120 and sem3_observed > time.time() - 3600)
            # queued_at=0 here has no queue-file provenance (this bypassed offline_queue.py
            # entirely) but the server treats it identically either way, per design — same
            # bounded event-time-only effect as scenario 2.
            check("direct POST: semantic.valid_from follows the queued_at BODY field the "
                  "same way replay does (consistent server-side contract)",
                  abs(sem3["valid_from"].timestamp() - 0.0) < 1.0)

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
