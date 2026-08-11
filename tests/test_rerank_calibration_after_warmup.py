"""issue #34: rerank calibration (core/rerank.py's prewarm()) must not measure
ms-per-pair WHILE the rest of boot's CPU-heavy synchronous work — pool setup, schema
DDL, the HNSW/GIN index warm-up probe (issue #59), MCP-mount thread startup — is still
running. Doing so produced a pessimistic/noisy snapshot (real field regression: 316ms/pair
measured after a threads=1->4 change that should have made it FASTER, not slower), which
can push derive_cap() below what the hardware actually warrants on a slower box (harmless
today only because MIN_CAP already floors it).

Before this fix, memnos_server.py's serve() called brain_rerank.prewarm_background() as
the FIRST thing it did — before POOL even existed. This fix moves that call to AFTER the
pool + schema + index warm-up block finishes, so calibration's own background thread no
longer races that work (it still runs off the request path — boot is not blocked on it).

This is an ORDERING claim, not a timing one, and it's deterministic by construction (not a
race to assert with a sleep): prewarm_background() is only ever CALLED once warm_indexes()
has already returned, so the "HNSW index warm-up complete" log line is written strictly
before the "rerank calibrated" line can be. Verified against a REAL subprocess boot (real
Postgres, real ONNX model load) by asserting that relative order in the captured stdout —
the same way test_recall_arm_degrade_http.py / test_coldstart_readiness_gate.py verify
real server behavior rather than mocking it.

Run: python tests/test_rerank_calibration_after_warmup.py
(spawns its own server; does not require one already running)
"""
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import psycopg
from psycopg.rows import dict_row

from core.control import Control
from core.store import BrainStore

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
PORT = int(os.environ.get("MEMNOS_CALIBRATION_ORDER_TEST_PORT", "8967"))
URL = f"http://127.0.0.1:{PORT}"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if (detail and not cond) else ""))
    PASS += bool(cond); FAIL += (not cond)


def _server_env():
    env = dict(os.environ, MEMNOS_DSN=DSN, MEMNOS_PORT=str(PORT))
    env["OPENAI_API_KEY"] = ""
    env.setdefault("MEMNOS_SECRET_KEY", "Y2FsaWJyYXRpb25fdGVzdF9rZXlfMzJiX2V4YWN0bHlfb2s=")
    return env


def wait_ready(timeout_s=60):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if urllib.request.urlopen(URL + "/readyz", timeout=2).status == 200:
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def stop_server(proc, timeout=15):
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def main():
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)
    BrainStore(conn=conn).create_schema("memnos", dim=384)

    logpath = os.path.join(ROOT, f".calibration_order_test_{PORT}.log")
    logf = open(logpath, "w")
    print(f"=== booting a dedicated memnos_server.py on :{PORT}, capturing stdout ===")
    proc = subprocess.Popen([sys.executable, "memnos_server.py"], cwd=ROOT, env=_server_env(),
                            stdout=logf, stderr=subprocess.STDOUT)
    try:
        check("server came up (/readyz)", wait_ready())

        # the calibration line is written by a background thread AFTER the listener
        # opens (real ONNX model load, not instant) — give it real time to appear
        # rather than assuming readiness implies it's already there.
        deadline = time.monotonic() + 30
        warm_idx = calib_idx = None
        lines = []
        while time.monotonic() < deadline and calib_idx is None:
            logf.flush()
            with open(logpath) as fh:
                lines = fh.readlines()
            for i, line in enumerate(lines):
                if warm_idx is None and "HNSW index warm-up complete" in line:
                    warm_idx = i
                if calib_idx is None and "rerank calibrated" in line:
                    calib_idx = i
            if calib_idx is None:
                time.sleep(0.5)

        check("the boot log records the HNSW index warm-up completing",
              warm_idx is not None, "".join(lines))
        check("the boot log records rerank calibration completing",
              calib_idx is not None, "".join(lines))
        check("calibration runs AFTER the index warm-up, not racing it during boot "
              "(issue #34 — this used to start before POOL even existed)",
              warm_idx is not None and calib_idx is not None and warm_idx < calib_idx,
              f"warm_idx={warm_idx} calib_idx={calib_idx}")

        if calib_idx is not None:
            m = re.search(r"measured_ms_per_pair=([\d.]+)", lines[calib_idx])
            check("the calibrated line reports a real measured_ms_per_pair value",
                  m is not None, lines[calib_idx])
    finally:
        stop_server(proc)
        logf.close()
        try:
            os.remove(logpath)
        except OSError:
            pass

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
