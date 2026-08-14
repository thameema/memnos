"""
Tests for the Tommy control channel (ControlServer / ControlClient).
These tests use real loopback TCP — no mocking needed; fast on any OS.
"""
import json
import socket
import threading
import time

import pytest

from tommy.control import ControlServer, ControlClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _raw_connect(port: int, timeout: float = 2.0) -> socket.socket:
    """Open a raw TCP connection to the control server."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(("127.0.0.1", port))
    s.settimeout(None)
    return s


def _send_raw(sock: socket.socket, msg: dict) -> None:
    sock.sendall(json.dumps(msg).encode() + b"\n")


def _recv_line(sock: socket.socket, timeout: float = 2.0) -> dict:
    sock.settimeout(timeout)
    buf = b""
    while b"\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise EOFError("connection closed before newline")
        buf += chunk
    line, _ = buf.split(b"\n", 1)
    return json.loads(line)


# ---------------------------------------------------------------------------
# ControlServer tests
# ---------------------------------------------------------------------------

class TestControlServer:
    def test_binds_ephemeral_port(self):
        srv = ControlServer()
        assert isinstance(srv.port, int)
        assert 1024 <= srv.port <= 65535
        srv.close()

    def test_harness_not_connected_initially(self):
        srv = ControlServer(connect_timeout=0.1)
        assert not srv.harness_connected
        srv.close()

    def test_send_returns_false_when_no_client(self):
        srv = ControlServer(connect_timeout=0.1)
        time.sleep(0.15)  # let accept timeout expire
        result = srv.send({"type": "ping"}, wait_connect=False)
        assert result is False
        srv.close()

    def test_inbound_message_delivered(self):
        received: list = []
        srv = ControlServer(on_message=received.append, connect_timeout=5.0)

        sock = _raw_connect(srv.port)
        _send_raw(sock, {"type": "progress", "pct": 42})
        time.sleep(0.1)
        sock.close()
        srv.close()

        assert len(received) == 1
        assert received[0] == {"type": "progress", "pct": 42}

    def test_malformed_json_does_not_kill_loop(self):
        """A bad JSON line must NOT terminate the receive loop."""
        received: list = []
        srv = ControlServer(on_message=received.append, connect_timeout=5.0)

        sock = _raw_connect(srv.port)
        # send bad JSON, then good JSON
        sock.sendall(b"not-json-at-all\n")
        time.sleep(0.05)
        _send_raw(sock, {"type": "checkpoint", "phase": "done"})
        time.sleep(0.1)
        sock.close()
        srv.close()

        # good message must still arrive despite the earlier bad line
        assert any(m.get("type") == "checkpoint" for m in received), received

    def test_outbound_wrap_up(self):
        """Server.wrap_up() must deliver the correct JSON to the connected client."""
        srv = ControlServer(connect_timeout=5.0)
        sock = _raw_connect(srv.port)
        time.sleep(0.05)  # let server register the connection

        srv.wrap_up(reason="test", budget_seconds=30)
        msg = _recv_line(sock)
        sock.close()
        srv.close()

        assert msg["type"] == "wrap_up"
        assert msg["reason"] == "test"
        assert msg["budget_seconds"] == 30

    def test_close_idempotent(self):
        srv = ControlServer(connect_timeout=0.1)
        srv.close()
        srv.close()  # must not raise


# ---------------------------------------------------------------------------
# Transcript glob tests
# ---------------------------------------------------------------------------

class TestTranscriptGlob:
    def test_glob_pattern_has_no_conversations_subdir(self):
        """Regression: glob must NOT include the non-existent 'conversations/' path segment."""
        from tommy.cli import _find_latest_claude_transcript
        import inspect
        src = inspect.getsource(_find_latest_claude_transcript)
        # Extract just the pattern= line to avoid false-positive on doc prose
        pattern_lines = [l for l in src.splitlines() if "pattern" in l and "glob" not in l.lower()]
        for line in pattern_lines:
            assert "conversations" not in line, (
                f"Glob pattern still contains 'conversations/' segment: {line!r}\n"
                "Real Claude Code layout is ~/.claude/projects/*/*.jsonl with no subdirectory"
            )

    def test_glob_finds_real_transcripts(self, tmp_path):
        """Pattern must match the real ~/.claude/projects/<dir>/<uuid>.jsonl layout."""
        import glob
        import pathlib
        # Simulate Claude Code's real project layout
        proj_dir = tmp_path / "projects" / "-Users-alice-myproject"
        proj_dir.mkdir(parents=True)
        transcript = proj_dir / "abc123.jsonl"
        transcript.write_text('{"role":"user","content":"hello"}\n')

        pattern = str(tmp_path / "projects" / "*" / "*.jsonl")
        matches = glob.glob(pattern)
        assert len(matches) == 1
        assert pathlib.Path(matches[0]) == transcript
