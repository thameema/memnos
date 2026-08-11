"""issue #34: rerank calibration (core/rerank.py's prewarm()) must not derive
effective_cap from a SINGLE boot-time ms-per-pair sample, which can be inflated by
concurrent CPU-heavy startup work racing that exact timed inference batch (real field
regression: 316ms/pair measured after a threads=1->4 change that should have made it
FASTER, not slower).

An earlier version of this fix moved brain_rerank.prewarm_background()'s call site in
memnos_server.py's serve() to AFTER the pool+schema+HNSW-index warm-up block (issue #59),
so the timed measurement would no longer overlap that work. That was reverted: prewarm()
is what actually loads the ONNX model in the first place, so delaying when it STARTS
measurably widens the window where the server answers but every recall serves degraded
(core/rerank.py's is_ready() stays False) — confirmed live, and identified as the actual,
previously-undiagnosed cause of a documented-but-unresolved CI flake in
tests/test_recall_arm_degrade_http.py's own baseline-not-degraded assertion (see PR #58's
history: that flake's real mechanism was never actually the arm failures it was chasing).
A calibration measurement being occasionally noisy is a smaller problem than every recall
in a widened window serving degraded — so prewarm_background() stays at its original
position (the first thing serve() does), and #34 is fixed a different way: prewarm() now
takes TWO timed samples and reports min(sample1, sample2) instead of a single snapshot —
noise can only INFLATE a sample, never deflate it below true cost, so the minimum of two
independent attempts rejects a contention-inflated outlier without needing to know WHEN
any contention happened, and without touching server boot ordering at all.

Verified here against a REAL subprocess boot (real Postgres, real ONNX model, real
threading — core/rerank.py's actual prewarm()/_measure_ms_per_pair() code path, not a
unit-level mock) using a test-only hook, MEMNOS_RERANK_SIMULATED_MS_PER_PAIR_SEQUENCE,
that returns a DIFFERENT forced latency on each successive call — proving prewarm()
genuinely takes the MINIMUM of its two samples, not the first, last, or an average.

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
PORT = int(os.environ.get("MEMNOS_CALIBRATION_TEST_PORT", "8967"))
URL = f"http://127.0.0.1:{PORT}"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if (detail and not cond) else ""))
    PASS += bool(cond); FAIL += (not cond)


def _server_env(sim_sequence):
    env = dict(os.environ, MEMNOS_DSN=DSN, MEMNOS_PORT=str(PORT))
    env["OPENAI_API_KEY"] = ""
    env.setdefault("MEMNOS_SECRET_KEY", "Y2FsaWJyYXRpb25fdGVzdF9rZXlfMzJiX2V4YWN0bHlfb2s=")
    env["MEMNOS_RERANK_SIMULATED_MS_PER_PAIR_SEQUENCE"] = sim_sequence
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


def _boot_and_read_calibration_line(logpath, sim_sequence):
    """Boot a dedicated server with the given forced sample sequence, wait for the
    'rerank calibrated' log line, and return it. Raises on timeout/crash."""
    logf = open(logpath, "w")
    proc = subprocess.Popen([sys.executable, "memnos_server.py"], cwd=ROOT,
                            env=_server_env(sim_sequence), stdout=logf, stderr=subprocess.STDOUT)
    try:
        up = wait_ready()
        if not up:
            logf.flush()
            with open(logpath) as fh:
                raise RuntimeError(f"server never became ready; log:\n{fh.read()}")
        deadline = time.monotonic() + 30
        calib_line = None
        while time.monotonic() < deadline and calib_line is None:
            logf.flush()
            with open(logpath) as fh:
                for line in fh:
                    if "rerank calibrated" in line:
                        calib_line = line
                        break
            if calib_line is None:
                time.sleep(0.3)
        if calib_line is None:
            with open(logpath) as fh:
                raise RuntimeError(f"'rerank calibrated' never appeared; log:\n{fh.read()}")
        return calib_line
    finally:
        stop_server(proc)
        logf.close()
        try:
            os.remove(logpath)
        except OSError:
            pass


def main():
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)
    BrainStore(conn=conn).create_schema("memnos", dim=384)

    print("=== SEQUENCE A: first sample slower (contention-shaped) — min must pick the second ===")
    line_a = _boot_and_read_calibration_line(
        os.path.join(ROOT, f".calib_test_a_{PORT}.log"), "80,30")
    print(f"  {line_a.strip()}")
    m = re.search(r"measured_ms_per_pair=([\d.]+)\s*\(samples=([\d.]+),([\d.]+)\)", line_a)
    check("boot log line has the expected 'measured_ms_per_pair=X (samples=Y,Z)' shape",
          m is not None, line_a)
    if m:
        reported, s1, s2 = float(m.group(1)), float(m.group(2)), float(m.group(3))
        check("both forced samples were actually taken, in order (80 then 30)",
              abs(s1 - 80.0) < 0.01 and abs(s2 - 30.0) < 0.01, f"samples={s1},{s2}")
        check("the reported measured_ms_per_pair is the MINIMUM of the two samples "
              "(30, not the first sample 80, not their average 55)",
              abs(reported - 30.0) < 0.01, f"reported={reported}")

    print("=== SEQUENCE B: first sample faster — min must NOT just always pick the second ===")
    line_b = _boot_and_read_calibration_line(
        os.path.join(ROOT, f".calib_test_b_{PORT}.log"), "20,90")
    print(f"  {line_b.strip()}")
    m2 = re.search(r"measured_ms_per_pair=([\d.]+)\s*\(samples=([\d.]+),([\d.]+)\)", line_b)
    check("second boot's log line also has the expected shape", m2 is not None, line_b)
    if m2:
        reported2, s1b, s2b = float(m2.group(1)), float(m2.group(2)), float(m2.group(3))
        check("both forced samples were actually taken, in order (20 then 90)",
              abs(s1b - 20.0) < 0.01 and abs(s2b - 90.0) < 0.01, f"samples={s1b},{s2b}")
        check("the reported measured_ms_per_pair is the MINIMUM of the two samples "
              "(20, the FIRST one this time — proves this isn't just 'always the second "
              "sample', it's a genuine min())",
              abs(reported2 - 20.0) < 0.01, f"reported={reported2}")

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
