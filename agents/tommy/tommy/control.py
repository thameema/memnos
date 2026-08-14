"""
Tommy control channel — TCP loopback IPC between Tommy and a running harness.

Design:
  - Tommy starts a ControlServer on 127.0.0.1:0 (OS picks the port)
  - Port is passed to the harness subprocess via TOMMY_CTRL_PORT env var
  - Wire format: newline-delimited JSON — works with any child language
    (Python, Node.js, Rust, Go, etc.)
  - Full duplex on one TCP connection

Tommy → harness (control messages):
  {"type": "wrap_up", "reason": "time_limit", "budget_seconds": 30}
  {"type": "abort"}
  {"type": "pivot", "new_goal": "focus only on auth bugs"}
  {"type": "status_request"}

Harness → Tommy (progress messages):
  {"type": "progress", "pct": 42, "detail": "step 3 done"}
  {"type": "checkpoint", "phase": "review", "summary": "found 3 issues"}
  {"type": "done", "summary": "all steps completed"}
  {"type": "error", "message": "..."}
  {"type": "question", "text": "Should I overwrite X?", "options": ["yes", "no"]}

Cross-platform: AF_INET 127.0.0.1 works on macOS, Linux, and Windows XP+.
No third-party dependencies.
"""
from __future__ import annotations

import json
import socket
import threading
import time
from typing import Callable, Optional


MessageHandler = Callable[[dict], None]


class ControlServer:
    """
    Lightweight TCP control server.  One instance per launched harness.

    Tommy creates this before Popen, passes self.port via TOMMY_CTRL_PORT
    env var, then calls send() to push control messages mid-run.
    Inbound messages (progress, checkpoints, questions) are delivered to
    the on_message callback on a daemon thread.
    """

    def __init__(
        self,
        on_message: Optional[MessageHandler] = None,
        connect_timeout: float = 30.0,
    ):
        """
        Args:
            on_message:       Called for each JSON message received from harness.
            connect_timeout:  Seconds to wait for harness to connect back.
                              After this the accept thread gives up (harness
                              may not support the control channel).
        """
        self._on_message = on_message or (lambda msg: None)
        self._connect_timeout = connect_timeout

        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(1)
        self._server.settimeout(connect_timeout)

        self.port: int = self._server.getsockname()[1]

        self._client: Optional[socket.socket] = None
        self._connected = threading.Event()
        self._send_lock = threading.Lock()
        self._closed = False

        self._accept_thread = threading.Thread(
            target=self._accept_loop,
            daemon=True,
            name=f"tommy-ctrl-accept-{self.port}",
        )
        self._accept_thread.start()

    # ── inbound (harness → Tommy) ─────────────────────────────────────────

    def _accept_loop(self) -> None:
        try:
            conn, addr = self._server.accept()
        except (OSError, TimeoutError):
            # Harness didn't connect — control channel not supported.
            return
        finally:
            # Stop accepting; we only ever expect one harness per server.
            try:
                self._server.close()
            except OSError:
                pass

        self._client = conn
        self._connected.set()
        buf = b""
        try:
            while True:
                try:
                    chunk = conn.recv(4096)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                        self._on_message(msg)
                    except json.JSONDecodeError as exc:
                        # Malformed line — log and continue.
                        print(
                            f"[tommy-ctrl] bad JSON from harness: {exc}: {line!r}",
                            flush=True,
                        )
        finally:
            try:
                conn.close()
            except OSError:
                pass

    # ── outbound (Tommy → harness) ────────────────────────────────────────

    def send(self, msg: dict, wait_connect: bool = True, timeout: float = 5.0) -> bool:
        """
        Send a control message to the running harness.  Thread-safe.

        Args:
            msg:          JSON-serialisable dict.
            wait_connect: If True and harness hasn't connected yet, block
                          up to `timeout` seconds for it to connect.
            timeout:      How long to wait for connection before giving up.

        Returns:
            True  — message sent successfully.
            False — harness not connected or connection lost.
        """
        if wait_connect:
            self._connected.wait(timeout=timeout)

        with self._send_lock:
            if self._client is None:
                return False
            try:
                self._client.sendall(json.dumps(msg).encode() + b"\n")
                return True
            except OSError:
                return False

    def wrap_up(self, reason: str = "user_request", budget_seconds: int = 60) -> bool:
        """Ask the harness to finish gracefully within budget_seconds."""
        return self.send({
            "type": "wrap_up",
            "reason": reason,
            "budget_seconds": budget_seconds,
        })

    def abort(self) -> bool:
        """Tell the harness to stop immediately."""
        return self.send({"type": "abort"})

    def pivot(self, new_goal: str) -> bool:
        """Redirect the harness to a new goal mid-run."""
        return self.send({"type": "pivot", "new_goal": new_goal})

    def answer(self, text: str) -> bool:
        """Reply to a question the harness sent via {type: 'question'}."""
        return self.send({"type": "answer", "text": text})

    # ── lifecycle ─────────────────────────────────────────────────────────

    @property
    def harness_connected(self) -> bool:
        """True once the harness has connected back to the control port."""
        return self._connected.is_set()

    def close(self) -> None:
        """Shut down the control channel.  Idempotent."""
        self._closed = True
        try:
            self._server.close()
        except OSError:
            pass
        if self._client:
            try:
                self._client.close()
            except OSError:
                pass


# ── Client helper (for harness-side Python code) ─────────────────────────────

class ControlClient:
    """
    Harness-side control client.

    Usage in a Python harness:

        client = ControlClient(on_control=handle_control_msg)
        client.progress(50, "halfway done")
        client.checkpoint("review", "found 3 issues")
        client.done("all steps completed")

    The on_control callback receives messages Tommy sends (wrap_up, abort, pivot).
    """

    def __init__(
        self,
        on_control: Optional[MessageHandler] = None,
        port: Optional[int] = None,
        connect_timeout: float = 5.0,
    ):
        import os
        port = port or int(os.environ.get("TOMMY_CTRL_PORT", 0))
        if not port:
            raise RuntimeError(
                "TOMMY_CTRL_PORT not set — harness was not launched by Tommy "
                "or the control channel is disabled."
            )

        self._on_control = on_control or (lambda msg: None)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.settimeout(connect_timeout)
        self._sock.connect(("127.0.0.1", port))
        self._sock.settimeout(None)
        self._lock = threading.Lock()

        self._recv_thread = threading.Thread(
            target=self._recv_loop, daemon=True, name="tommy-ctrl-recv"
        )
        self._recv_thread.start()

    def _recv_loop(self) -> None:
        buf = b""
        try:
            while True:
                try:
                    chunk = self._sock.recv(4096)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if line:
                        try:
                            self._on_control(json.loads(line))
                        except json.JSONDecodeError:
                            pass
        finally:
            try:
                self._sock.close()
            except OSError:
                pass

    def _send(self, msg: dict) -> None:
        with self._lock:
            try:
                self._sock.sendall(json.dumps(msg).encode() + b"\n")
            except OSError:
                pass

    def progress(self, pct: int, detail: str = "") -> None:
        self._send({"type": "progress", "pct": pct, "detail": detail})

    def checkpoint(self, phase: str, summary: str) -> None:
        self._send({"type": "checkpoint", "phase": phase, "summary": summary})

    def done(self, summary: str = "") -> None:
        self._send({"type": "done", "summary": summary})

    def error(self, message: str) -> None:
        self._send({"type": "error", "message": message})

    def question(self, text: str, options: list[str] | None = None) -> None:
        msg: dict = {"type": "question", "text": text}
        if options:
            msg["options"] = options
        self._send(msg)

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass
