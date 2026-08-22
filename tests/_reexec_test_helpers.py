"""Shared helpers for the issue #68 self-re-exec test files
(test_mcp_adapter_reexec.py, test_mcp_adapter_reexec_escape_hatch.py,
test_mcp_adapter_reexec_drain.py). NOT itself a test — doesn't match tests/test_*.py,
so `make test` / CI's server-tests loop never tries to execute it directly.

Deliberately does NOT reuse `mcp.client.stdio.stdio_client()` for the JSON-RPC side:
that helper spawns its subprocess via anyio internally and never exposes the resulting
process object (pid / poll()) to the caller, which these tests need to prove "same PID
before and after" and "the parent process never sees the child exit". StdioRPC below is
a minimal, dependency-free (stdlib only) hand-rolled MCP JSON-RPC-over-stdio client —
newline-delimited JSON-RPC 2.0, the same wire format `mcp/server/stdio.py` and
`mcp/client/stdio/__init__.py` both use — built directly on `subprocess.Popen` so the
real OS process handle is always in hand.
"""
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MEMNOS_MCP_SRC = os.path.join(ROOT, "memnos_mcp.py")
OFFLINE_QUEUE_SRC = os.path.join(ROOT, "offline_queue.py")

# Appended into a COPY of memnos_mcp.py to simulate `memnos upgrade` shipping new code:
# a tool that exists ONLY in the "new" build. Its presence (not just a stray log line or
# an unchanged pid) is the proof that a re-exec'd process is genuinely running new code —
# calling it before the simulated upgrade fails with "Unknown tool" (ToolError), calling
# it after a real re-exec succeeds.
MARKER_TOOL_SRC = '''

@mcp.tool()
def _reexec_marker() -> str:
    """Test-only marker tool that exists ONLY in the simulated 'upgraded' build."""
    return "REEXEC_MARKER_PRESENT"

'''


def make_fake_install(tmp_root: str) -> str:
    """Copy just the two source files memnos_mcp.py actually needs at import time
    (memnos_mcp.py, offline_queue.py — nsresolve.py is optional/best-effort, see its
    try/except at the top of memnos_mcp.py) into an isolated directory, so "simulating
    an upgrade" (mutating memnos_mcp.py) never touches this checkout's real working
    tree."""
    install_dir = os.path.join(tmp_root, "install")
    os.makedirs(install_dir, exist_ok=True)
    shutil.copy(MEMNOS_MCP_SRC, os.path.join(install_dir, "memnos_mcp.py"))
    shutil.copy(OFFLINE_QUEUE_SRC, os.path.join(install_dir, "offline_queue.py"))
    return install_dir


def simulate_upgrade(install_dir: str) -> None:
    """Mutate the fake install's memnos_mcp.py the way a real `memnos upgrade` would
    swap files under a running process: new content, new mtime, new size. Inserted
    BEFORE the `if __name__ == "__main__":` guard (not appended at EOF) because
    run_stdio() blocks forever inside that guard — code appended after it would never
    even get a chance to execute on the re-exec'd run."""
    path = os.path.join(install_dir, "memnos_mcp.py")
    src = open(path, encoding="utf-8").read()
    anchor = 'if __name__ == "__main__":'
    idx = src.index(anchor)
    assert idx > 0, "memnos_mcp.py layout changed — update the anchor"
    new_src = src[:idx] + MARKER_TOOL_SRC + src[idx:]
    with open(path, "w", encoding="utf-8") as f:
        f.write(new_src)
    # Belt-and-suspenders: force the mtime forward too, in case the filesystem's clock
    # resolution is coarse enough that the content-length change alone might land in
    # the same tick as the original write.
    st = os.stat(path)
    os.utime(path, (st.st_atime, st.st_mtime + 2))


def make_stub_handler(remember_delay: float = 0.0, remember_turn_id: int = 1):
    """Build a minimal stand-in for the real memnos HTTP server: just enough of
    POST /remember and POST /recall for memnos_mcp.py's remember()/recall() tools to
    round-trip successfully. `remember_delay` simulates a slow upstream call so a test
    can hold a tool call genuinely in-flight for a controlled duration."""

    class Stub(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/remember" or self.path == "/memory/write":
                if remember_delay:
                    time.sleep(remember_delay)
                resp = {"namespace": body.get("namespace"), "turn_id": remember_turn_id,
                        "facts": 0, "extraction": "queued"}
            elif self.path == "/recall":
                resp = {"context": f"(stub context for: {body.get('query')})", "memories": []}
            else:
                resp = {}
            data = json.dumps(resp).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    return Stub


def start_stub(handler_cls):
    srv = HTTPServer(("127.0.0.1", 0), handler_cls)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{port}"


def build_env(url, token, ns, home, interval="1", grace="0.2", reexec=None):
    env = dict(os.environ)
    env.pop("OPENAI_API_KEY", None)          # tests must never accidentally hit a real LLM
    env["HOME"] = home
    env["MEMNOS_URL"] = url
    env["MEMNOS_TOKEN"] = token
    env["MEMNOS_NS"] = ns
    env["MEMNOS_ADAPTER_REEXEC_INTERVAL_S"] = str(interval)
    env["MEMNOS_ADAPTER_REEXEC_DRAIN_GRACE_S"] = str(grace)
    if reexec is not None:
        env["MEMNOS_ADAPTER_REEXEC"] = str(reexec)
    else:
        env.pop("MEMNOS_ADAPTER_REEXEC", None)
    return env


class StdioRPC:
    """Minimal MCP JSON-RPC-over-stdio client wrapping a real subprocess.Popen, so the
    test always has direct access to the real OS pid and can poll() it directly."""

    def __init__(self, script_path: str, env: dict, cwd: str | None = None):
        self.proc = subprocess.Popen(
            [sys.executable, script_path],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, cwd=cwd, text=True, bufsize=1,
        )
        self._id_lock = threading.Lock()
        self._next_id = 0
        # id -> single-slot Queue: several of these tests deliberately keep more than
        # one JSON-RPC request outstanding at once (a slow in-flight call + a fast
        # concurrent probe) over the SAME connection, which is legal MCP/JSON-RPC (ids
        # disambiguate). A single shared Queue drained by whichever thread happens to
        # call get() first would let one thread's wait_response() silently steal
        # another thread's response by id — this demultiplexes properly instead.
        self._waiters_lock = threading.Lock()
        self._waiters: dict[int, "queue.Queue"] = {}
        self._err_lines: list[str] = []
        self._err_lock = threading.Lock()
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()

    def _pump_stdout(self):
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rid = obj.get("id")
                q = None
                if rid is not None:
                    with self._waiters_lock:
                        q = self._waiters.get(rid)
                if q is not None:
                    q.put(obj)
                # else: no one is waiting on this id (an unsolicited notification, or a
                # response nobody ever asked to wait for) — nothing to do with it.
        except (ValueError, OSError):
            pass

    def _pump_stderr(self):
        try:
            for line in self.proc.stderr:
                with self._err_lock:
                    self._err_lines.append(line.rstrip("\n"))
        except (ValueError, OSError):
            pass

    def stderr_text(self) -> str:
        with self._err_lock:
            return "\n".join(self._err_lines)

    @property
    def pid(self) -> int:
        return self.proc.pid

    def alive(self) -> bool:
        return self.proc.poll() is None

    def _send(self, obj: dict) -> None:
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def send_request(self, method: str, params: dict | None = None) -> int:
        with self._id_lock:
            self._next_id += 1
            rid = self._next_id
        # Register the waiter BEFORE writing the request to stdin — otherwise a
        # response fast enough to arrive before this thread gets back around to
        # calling wait_response() would find no waiter registered yet and be dropped.
        with self._waiters_lock:
            self._waiters[rid] = queue.Queue(maxsize=1)
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        return rid

    def send_notification(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def wait_response(self, rid: int, timeout: float = 20) -> dict:
        with self._waiters_lock:
            q = self._waiters.get(rid)
        if q is None:
            raise RuntimeError(f"wait_response({rid}): no waiter registered — "
                                f"send_request() wasn't called for this id, or its "
                                f"response was already consumed")
        try:
            return q.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(
                f"no response for id={rid} within {timeout}s "
                f"(alive={self.alive()}, stderr tail: {self.stderr_text()[-2000:]})") from None
        finally:
            with self._waiters_lock:
                self._waiters.pop(rid, None)

    def initialize(self, timeout: float = 20) -> dict:
        rid = self.send_request("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "memnos-reexec-test", "version": "0"},
        })
        resp = self.wait_response(rid, timeout=timeout)
        if "error" in resp:
            raise RuntimeError(f"initialize failed: {resp['error']}")
        self.send_notification("notifications/initialized")
        return resp["result"]

    def send_call_tool(self, name: str, arguments: dict) -> int:
        return self.send_request("tools/call", {"name": name, "arguments": arguments})

    def call_tool(self, name: str, arguments: dict, timeout: float = 20) -> dict:
        rid = self.send_call_tool(name, arguments)
        return self.wait_response(rid, timeout=timeout)

    def close(self, timeout: float = 8) -> None:
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=timeout)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


def tool_text(resp: dict) -> str:
    """Extract the plain-text body of a tools/call JSON-RPC response."""
    result = resp.get("result") or {}
    content = result.get("content") or []
    return "".join(c.get("text", "") for c in content if isinstance(c, dict))


def tool_is_error(resp: dict) -> bool:
    result = resp.get("result") or {}
    return bool(result.get("isError"))
