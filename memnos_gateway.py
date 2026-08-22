"""memnos gateway — issue #37 Layer 2: zero-downtime blue-green upgrade front door.

Problem this closes: `memnos restart`/`memnos upgrade` used to be a hard stop-then-start
against the SAME public port (:8900 by default) — a real window where the port refuses
connections. Every REST/HTTP client (and the streamable-HTTP MCP mount at /mcp, issue
#37 Layer 1) sees that window as connection failures.

Design (see memnos_cli.py's `_rolling_upgrade_or_convert` for the CLI side):
  - This process binds the PUBLIC port and NEVER re-binds it again for its whole life —
    that socket is the one promise this module exists to keep.
  - The real memnos server (memnos_server.py's `serve()`, completely UNCHANGED by this
    issue) always runs as a separate child process, bound to an INTERNAL, ephemeral,
    loopback-only port picked fresh each time (`_pick_free_port`) — never the public one.
  - Every inbound request (any path, any method) is reverse-proxied to whichever internal
    port is currently "live" (`_current_port`, a plain module global — read/write of a
    single reference is atomic under the GIL, so a request that snapshots it at the top
    of `_forward` is safe against a concurrent flip; see `_forward` below). This is the
    SAME buffer-then-relay technique memnos_server.py's `Handler._forward_mcp` already
    uses for the internal MCP mount (issue #37 Layer 1) — reused here, not reinvented,
    generalized from one path (/mcp) to all of them.
  - A reserved control namespace under `/__gateway__/` (never a real memnos route — every
    real route is unprefixed or under /admin, /user, /mcp) exposes `status` and `upgrade`,
    bearer-token authed with a random per-boot token nobody outside this host's CLI ever
    sees (written to `~/.memnos/gateway_state.json`, mode 0600).
  - `POST /__gateway__/upgrade` starts a BACKGROUND rolling upgrade and returns
    immediately (202) — it must never block the caller for the ~seconds-to-minutes a real
    prewarm+drain cycle takes; the caller polls `GET /__gateway__/status` instead. The
    rolling upgrade itself:
      1. picks a fresh internal port, spawns a NEW backend there
      2. polls that backend's OWN `/readyz` — reusing the server's existing
         `_READYZ_WARM_PROVEN` HNSW-warm gate (issues #59/#31/#12) as the sole readiness
         signal; this module does not reimplement warmup detection
      3. on failure (bad exit, or never ready within the timeout): kills the new backend
         and reports `status: failed` — `_current_port` is untouched, so the OLD backend
         keeps serving every request throughout, and nothing is half-swapped
      4. on success: atomically flips `_current_port` to the new backend — from this
         instant every NEW inbound request goes to the new backend; requests already
         in flight to the old one keep running against it (see in-flight accounting)
      5. drains: waits (bounded by MEMNOS_GATEWAY_DRAIN_TIMEOUT_S) for the in-flight
         counter on the OLD port to hit zero
      6. only THEN signals the old backend (SIGTERM, grace period, SIGKILL if it doesn't
         exit) — it is never touched while it might still be serving a request that
         started before the flip

No streaming/SSE concern: every memnos response this proxies is a normal bounded
request/response (verified — the /mcp mount itself requires Content-Length, not chunked,
and there is no other long-lived streaming endpoint in this codebase), so buffering the
full response before relaying (rather than a byte-by-byte streaming relay) is safe and
keeps this at parity with the established `_forward_mcp` pattern instead of adding new
streaming-proxy machinery this codebase doesn't otherwise need.
"""
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".memnos")
STATE_PATH = os.path.join(CONFIG_DIR, "gateway_state.json")

CONTROL_PREFIX = "/__gateway__/"

# Tunables — env-overridable so a CI run (or an operator who wants a tighter/looser
# budget) doesn't need a code change. Defaults are deliberately generous: a first-ever
# backend boot on a machine with no cached embedding/reranker model can legitimately take
# minutes (see memnos_cli.py's `_serve_background`, which budgets ~6 min for the same
# reason) — a rolling upgrade must not time out and report a false failure just because
# the box is slow, since a false failure here is harmless (the old backend is still
# serving) but a falsely SHORT timeout would make real slow-box upgrades unusable.
READY_TIMEOUT_S = float(os.environ.get("MEMNOS_GATEWAY_READY_TIMEOUT_S", "300"))
DRAIN_TIMEOUT_S = float(os.environ.get("MEMNOS_GATEWAY_DRAIN_TIMEOUT_S", "30"))
KILL_GRACE_S = float(os.environ.get("MEMNOS_GATEWAY_KILL_GRACE_S", "10"))

# ---- process-wide state (this module IS the long-lived gateway process) -------------
_public_port = None
_control_token = None
_current_port = None            # internal port of the LIVE backend — the "atomic flip"
_current_proc = None            # its subprocess.Popen handle

_inflight = {}                  # internal_port -> in-flight request count
_inflight_lock = threading.Lock()

_upgrade_lock = threading.Lock()
_upgrade_state = {"status": "idle"}   # snapshot exposed via /__gateway__/status


# ---- process helpers ------------------------------------------------------------------
def _set_proc_title(title):
    try:
        import setproctitle
        setproctitle.setproctitle(title)
    except Exception:
        pass


def _pick_free_port():
    """OS-assigned ephemeral loopback port. Same bind-probe-close technique
    memnos_server.py itself documents using elsewhere (start_mcp_http_mount) — a fixed
    offset is a real collision hazard, ephemeral binding can't collide with anything
    already listening. The bind-then-close-then-hand-to-a-child window is a real but tiny
    (sub-millisecond, loopback-only) race; if a new backend loses it, that's just a normal
    startup failure (an unrelated process happened to grab the same ephemeral port in that
    instant) and is handled exactly like any other prewarm/start failure below — the old
    backend is never touched."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _spawn_backend(port):
    """Launch a real memnos_server.py `serve()` instance bound to an INTERNAL port —
    completely unmodified Layer-1/Layer-3 code, just told to bind somewhere other than
    the public port. Same exe-resolution fallback memnos_cli.py's `_serve_background`
    uses (installed `memnos` entry point, else invoke this checkout's CLI directly)."""
    exe = shutil.which("memnos")
    if exe:
        cmd = [exe, "serve", "--port", str(port)]
    else:
        cli_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memnos_cli.py")
        cmd = [sys.executable, cli_path, "serve", "--port", str(port)]
    env = dict(os.environ)
    env["MEMNOS_PORT"] = str(port)          # belt + suspenders alongside --port above
    # No stdout/stderr override: inherit this process's own fds. When THIS process was
    # itself launched in the background with stdout/stderr redirected to server.log (see
    # memnos_cli.py's `_start_gateway_background`), every backend's output lands in that
    # same log automatically; in the foreground it goes to the same terminal.
    return subprocess.Popen(cmd, env=env, start_new_session=True, stdin=subprocess.DEVNULL)


def _wait_ready(port, timeout, proc=None):
    """Poll GET /readyz on `port` until it reports ready or `timeout` elapses. This is
    the ENTIRE readiness signal — memnos_server.py's /readyz already proves both "the
    connection pool is alive" and "the HNSW vector indexes are warm"
    (`_READYZ_WARM_PROVEN`, issues #59/#31/#12); reusing it here means a rolling upgrade
    can never flip traffic onto a backend that would serve a cold/degraded first recall.
    Bails out immediately (returns False without waiting out the rest of `timeout`) if
    `proc` has already exited — a crash-looping backend (e.g. a bad import, a stale
    checkout shadowing the real package — the exact failure mode a real incident hit
    against the live server) must be reported as a fast, clear failure, not a slow one."""
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/readyz"
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            return False
        try:
            r = httpx.get(url, timeout=2)
            if r.status_code == 200 and r.json().get("ready"):
                return True
        except httpx.TransportError:
            pass
        except Exception:
            pass
        time.sleep(0.3)
    return False


def _wait_drain(port, timeout):
    """Block until the in-flight counter for `port` reaches zero or `timeout` elapses.
    Returns True if it fully drained, False if the timeout fired first (the caller still
    proceeds to signal the backend after a timeout — an indefinite hang on one stuck
    request must not wedge every future upgrade forever; see `_run_rolling_upgrade`)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with _inflight_lock:
            n = _inflight.get(port, 0)
        if n <= 0:
            return True
        time.sleep(0.1)
    with _inflight_lock:
        return _inflight.get(port, 0) <= 0


def _inflight_incr(port):
    with _inflight_lock:
        _inflight[port] = _inflight.get(port, 0) + 1


def _inflight_decr(port):
    with _inflight_lock:
        _inflight[port] = max(0, _inflight.get(port, 0) - 1)


def _kill(proc, grace=None):
    """SIGTERM, wait, SIGKILL if it didn't exit — never called until the caller has
    already confirmed (or timed out waiting for) drain, so this never interrupts a
    request that was in flight when the flip happened."""
    if proc is None or proc.poll() is not None:
        return
    grace = KILL_GRACE_S if grace is None else grace
    try:
        proc.terminate()
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass
    except Exception:
        pass


# ---- state file (how memnos_cli.py's `restart`/`upgrade` find this gateway later) ----
def _write_state(state):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = STATE_PATH + f".tmp{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE_PATH)


def _clear_state():
    try:
        os.remove(STATE_PATH)
    except OSError:
        pass


# ---- rolling (blue-green) upgrade ----------------------------------------------------
def _status_snapshot():
    with _inflight_lock:
        inflight = dict(_inflight)
    proc = _current_proc
    with _upgrade_lock:
        upgrade = dict(_upgrade_state)
    return {
        "public_port": _public_port,
        "current_backend_port": _current_port,
        "current_backend_pid": proc.pid if proc else None,
        "inflight": inflight,
        "upgrade": upgrade,
        "gateway_pid": os.getpid(),
    }


def _start_rolling_upgrade():
    """Kick off a rolling upgrade on a background thread; returns the Thread, or None if
    one is already running (control endpoint reports 202/'already running' either way —
    starting a SECOND concurrent upgrade would race two backends for `_current_port`)."""
    with _upgrade_lock:
        if _upgrade_state.get("status") == "running":
            return None
        _upgrade_state.clear()
        _upgrade_state.update(status="running", phase="spawning", started_at=time.time())
    t = threading.Thread(target=_run_rolling_upgrade, name="memnos-gateway-upgrade", daemon=True)
    t.start()
    return t


def _set_upgrade(**kv):
    with _upgrade_lock:
        _upgrade_state.update(**kv)


def _run_rolling_upgrade():
    global _current_port, _current_proc
    old_port, old_proc = _current_port, _current_proc
    try:
        new_port = _pick_free_port()
        new_proc = _spawn_backend(new_port)
    except Exception as e:
        _set_upgrade(status="failed", phase="spawn_failed",
                     error=f"{type(e).__name__}: {e}", finished_at=time.time())
        print(f"[memnos-gateway] upgrade FAILED to spawn a new backend: {type(e).__name__}: {e} "
              f"— old backend (pid {old_proc.pid if old_proc else '?'}) unaffected, still serving.",
              flush=True)
        return

    _set_upgrade(phase="prewarm", new_port=new_port, new_pid=new_proc.pid)
    print(f"[memnos-gateway] upgrade: new backend pid {new_proc.pid} on internal port "
          f"{new_port} — waiting for /readyz ...", flush=True)
    ok = _wait_ready(new_port, READY_TIMEOUT_S, new_proc)
    if not ok:
        _kill(new_proc)
        _set_upgrade(status="failed", phase="prewarm_failed",
                     error=f"new backend (pid {new_proc.pid}) never became ready within "
                           f"{READY_TIMEOUT_S:.0f}s (or crashed on startup)",
                     finished_at=time.time())
        print(f"[memnos-gateway] upgrade FAILED: new backend never became ready — killed it. "
              f"OLD backend (pid {old_proc.pid if old_proc else '?'}, port {old_port}) was NEVER "
              f"touched and is still serving all traffic. Nothing was swapped.", flush=True)
        return

    # ---- the atomic flip: from this line on, every NEW request goes to new_port -------
    swap_time = time.time()
    _current_port, _current_proc = new_port, new_proc
    _set_upgrade(phase="draining", old_port=old_port,
                old_pid=(old_proc.pid if old_proc else None), swapped_at=swap_time)
    print(f"[memnos-gateway] upgrade: flipped traffic to pid {new_proc.pid} (port {new_port}) — "
          f"draining old backend (pid {old_proc.pid if old_proc else '?'}, port {old_port}) ...",
          flush=True)

    drained = _wait_drain(old_port, DRAIN_TIMEOUT_S) if old_port is not None else True
    if not drained:
        print(f"[memnos-gateway] WARNING: old backend (port {old_port}) did not fully drain "
              f"within {DRAIN_TIMEOUT_S:.0f}s — stopping it anyway (a single stuck request must "
              f"not wedge every future upgrade).", flush=True)
    if old_proc is not None:
        _kill(old_proc)

    _set_upgrade(status="done", phase="complete", drained=drained,
                finished_at=time.time())
    print(f"[memnos-gateway] upgrade COMPLETE — live backend is now pid {new_proc.pid} "
          f"(port {new_port}); old backend stopped (drained={drained}).", flush=True)


# ---- the gateway's HTTP surface --------------------------------------------------------
class GatewayHandler(BaseHTTPRequestHandler):
    server_version = "memnos-gateway/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send_json(self, code, obj):
        body = json.dumps(obj, default=str).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _authed(self):
        h = self.headers.get("Authorization", "")
        tok = h[7:].strip() if h.lower().startswith("bearer ") else ""
        return bool(tok) and bool(_control_token) and secrets.compare_digest(tok, _control_token)

    def _control(self, method):
        if not self._authed():
            n = int(self.headers.get("Content-Length", 0) or 0)
            if n:
                self.rfile.read(n)
            return self._send_json(401, {"error": "unauthorized"})
        if self.path == CONTROL_PREFIX + "status" and method == "GET":
            return self._send_json(200, _status_snapshot())
        if self.path == CONTROL_PREFIX + "upgrade" and method == "POST":
            n = int(self.headers.get("Content-Length", 0) or 0)
            if n:
                self.rfile.read(n)          # no request body params today — discard
            t = _start_rolling_upgrade()
            if t is None:
                return self._send_json(202, {"status": "already running", **_status_snapshot()})
            return self._send_json(202, {"status": "started"})
        return self._send_json(404, {"error": "unknown gateway control route"})

    def _forward(self, method):
        """Reverse-proxy to whichever internal backend is currently live — the SAME
        buffer-then-relay shape as memnos_server.py's Handler._forward_mcp, generalized
        from one path to all of them (see module docstring for why buffering is safe
        here: nothing this proxies is a long-lived stream)."""
        port = _current_port
        if port is None:
            return self._send_json(503, {"error": "memnos gateway: no backend ready yet "
                                         "(starting up)"})
        n = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(n) if n else b""
        fwd_headers = {k: v for k, v in self.headers.items()
                      if k.lower() not in ("host", "content-length", "connection")}
        url = f"http://127.0.0.1:{port}{self.path}"
        _inflight_incr(port)
        try:
            try:
                r = httpx.request(method, url, content=body, headers=fwd_headers, timeout=120)
            except httpx.TransportError as e:
                return self._send_json(502, {"error": f"memnos gateway: backend unreachable "
                                             f"({type(e).__name__})"})
            try:
                self.send_response(r.status_code)
                for hk, hv in r.headers.items():
                    if hk.lower() in ("content-length", "connection", "transfer-encoding"):
                        continue
                    self.send_header(hk, hv)
                self.send_header("Content-Length", str(len(r.content)))
                self.end_headers()
                self.wfile.write(r.content)
            except (BrokenPipeError, ConnectionResetError):
                pass
        finally:
            _inflight_decr(port)

    def do_GET(self):
        if self.path.startswith(CONTROL_PREFIX):
            return self._control("GET")
        return self._forward("GET")

    def do_POST(self):
        if self.path.startswith(CONTROL_PREFIX):
            return self._control("POST")
        return self._forward("POST")

    def do_DELETE(self):
        if self.path.startswith(CONTROL_PREFIX):
            return self._control("DELETE")
        return self._forward("DELETE")

    def do_PUT(self):
        return self._forward("PUT")

    def do_PATCH(self):
        return self._forward("PATCH")


def run(public_port):
    """Boot + run the gateway. Blocks until SIGTERM/SIGINT. Importable so `memnos gateway`
    (memnos_cli.py's cmd_gateway) reuses it, same convention as memnos_server.serve()."""
    global _public_port, _control_token, _current_port, _current_proc
    _set_proc_title("memnos-gateway")
    _public_port = public_port
    _control_token = secrets.token_hex(32)

    httpd = ThreadingHTTPServer(("127.0.0.1", public_port), GatewayHandler)
    print(f"[memnos-gateway] zero-downtime front door listening on http://127.0.0.1:{public_port} "
          f"(issue #37 Layer 2)", flush=True)
    threading.Thread(target=httpd.serve_forever, name="memnos-gateway-http", daemon=True).start()

    # State file written as soon as the public listener is actually up — before the
    # initial backend is ready — so a concurrent `memnos status`/`restart` sees a real,
    # reachable gateway (its /__gateway__/status will just report current_backend_port:
    # null and every request will 503 "starting up" until the backend boots below).
    _write_state({"port": public_port, "control_token": _control_token,
                 "gateway_pid": os.getpid(), "started_at": time.time()})

    print("[memnos-gateway] starting initial backend ...", flush=True)
    try:
        port = _pick_free_port()
        proc = _spawn_backend(port)
    except Exception as e:
        print(f"[memnos-gateway] FATAL: could not spawn the initial backend: "
              f"{type(e).__name__}: {e}", flush=True)
        _clear_state()
        httpd.shutdown()
        sys.exit(1)
    ok = _wait_ready(port, READY_TIMEOUT_S, proc)
    if not ok:
        print(f"[memnos-gateway] FATAL: initial backend (pid {proc.pid}, internal port {port}) "
              f"never became ready within {READY_TIMEOUT_S:.0f}s — see its output above for why.",
              flush=True)
        _kill(proc)
        _clear_state()
        httpd.shutdown()
        sys.exit(1)
    _current_port, _current_proc = port, proc
    print(f"[memnos-gateway] initial backend ready — pid {proc.pid}, internal port {port}. "
          f"Serving.", flush=True)

    stop_evt = threading.Event()

    def _on_signal(signum, frame):
        print(f"[memnos-gateway] received signal {signum} — shutting down ...", flush=True)
        stop_evt.set()

    # Registered on the main thread (required by Python) — this function IS the main
    # thread's own call, so this is always safe here.
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    stop_evt.wait()

    httpd.shutdown()
    if _current_proc is not None:
        _kill(_current_proc)
    _clear_state()
    print("[memnos-gateway] stopped.", flush=True)


if __name__ == "__main__":
    run(int(os.environ.get("MEMNOS_PORT", "8900")))
