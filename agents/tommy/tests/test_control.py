"""
Tests for the Tommy control channel (ControlServer / ControlClient).
These tests use real loopback TCP — no mocking needed; fast on any OS.
"""
import json
from pathlib import Path
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


# ---------------------------------------------------------------------------
# SDK API contract tests — guard against calling non-existent SDK methods
# ---------------------------------------------------------------------------

import inspect
import sys


def _tommy_uses_only_valid_sdk_methods(source_file: str, valid_methods: set) -> list[str]:
    """Parse source for client.<method>() calls and report any not in valid_methods."""
    import re
    text = Path(source_file).read_text()
    found = re.findall(r"client\.(\w+)\(", text)
    return [m for m in found if m not in valid_methods]


SDK_METHODS = {
    # MemnosClient public API (sync)
    "remember", "recall", "ingest_file", "context", "consolidate",
    "feedback", "healthy", "close",
}

TOMMY_SOURCES = [
    Path(__file__).parent.parent / "tommy" / "cli.py",
    Path(__file__).parent.parent / "tommy" / "mcp_server.py",
]


class TestSDKApiContract:
    """Ensure Tommy only calls SDK methods that actually exist."""

    def test_no_invalid_sdk_calls_in_cli(self):
        invalid = _tommy_uses_only_valid_sdk_methods(str(TOMMY_SOURCES[0]), SDK_METHODS)
        assert invalid == [], (
            f"cli.py calls non-existent SDK methods: {invalid}. "
            "Check MemnosClient in sdk/memnos_sdk/client.py."
        )

    def test_no_invalid_sdk_calls_in_mcp_server(self):
        invalid = _tommy_uses_only_valid_sdk_methods(str(TOMMY_SOURCES[1]), SDK_METHODS)
        assert invalid == [], (
            f"mcp_server.py calls non-existent SDK methods: {invalid}. "
            "Check MemnosClient in sdk/memnos_sdk/client.py."
        )

    def test_recall_uses_fact_quota_not_limit(self):
        """recall() has no 'limit' param — must use fact_quota."""
        import re
        for src in TOMMY_SOURCES:
            text = src.read_text()
            bad = re.findall(r"client\.recall\([^)]*\blimit\s*=[^=][^)]*\)", text)
            assert bad == [], f"{src.name} calls recall() with 'limit=': {bad}"

    def test_remember_has_no_memory_type_param(self):
        """remember() has no 'memory_type' param."""
        import re
        for src in TOMMY_SOURCES:
            text = src.read_text()
            bad = re.findall(r"client\.remember\([^)]*memory_type[^)]*\)", text)
            assert bad == [], f"{src.name} calls remember() with 'memory_type=': {bad}"


# ---------------------------------------------------------------------------
# mcp_server structural tests — sync dispatch correctness
# ---------------------------------------------------------------------------

class TestMcpServerStructure:
    """Structural checks on mcp_server.py that don't require a live harness."""

    def test_sync_dispatch_joins_drain_before_tail(self):
        """
        When async_run=False, the sync dispatch path must join the drain
        thread before calling tail() — otherwise output is truncated on
        fast-exiting processes.
        """
        import re
        src = (Path(__file__).parent.parent / "tommy" / "mcp_server.py").read_text()
        # Find the sync block: proc.wait() … tail(200)
        # drain.join() must appear BETWEEN proc.wait() and tail(200)
        # We locate the positions of all three in the source text.
        wait_pos = src.find("proc.wait()")
        join_pos = src.find("drain.join(", wait_pos)
        tail_pos = src.find("t.tail(200)", wait_pos)
        assert wait_pos != -1, "proc.wait() not found in mcp_server.py"
        assert join_pos != -1, (
            "drain.join() missing after proc.wait() — sync dispatch will return "
            "truncated output on fast-exiting processes"
        )
        assert join_pos < tail_pos, (
            f"drain.join() (pos {join_pos}) must come before t.tail(200) (pos {tail_pos})"
        )

    def test_sync_dispatch_closes_ctrl_before_return(self):
        """
        When async_run=False, ctrl.close() must be called to release the
        control channel socket — otherwise sockets/threads leak per sync dispatch.
        """
        import re
        src = (Path(__file__).parent.parent / "tommy" / "mcp_server.py").read_text()
        wait_pos = src.find("proc.wait()")
        close_pos = src.find("ctrl.close()", wait_pos)
        tail_pos = src.find("t.tail(200)", wait_pos)
        assert close_pos != -1, (
            "ctrl.close() not called after proc.wait() — ControlServer leaks "
            "socket + accept thread on every sync dispatch"
        )
        assert close_pos < tail_pos, (
            "ctrl.close() must be called before returning tail output"
        )

    def test_module_docstring_tool_count(self):
        """Module docstring tool count must match the number of @mcp.tool() decorators."""
        import re
        src = (Path(__file__).parent.parent / "tommy" / "mcp_server.py").read_text()
        # Count @mcp.tool() decorators
        actual = len(re.findall(r"@mcp\.tool\(\)", src))
        # Extract the number from the docstring  "Eight tools:" / "Seven tools:" etc.
        m = re.search(r"(\w+) tools?:", src[:500])
        assert m is not None, "Could not find 'N tools:' in module docstring"
        word_to_int = {
            "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
            "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
        }
        stated = word_to_int.get(m.group(1).lower())
        assert stated == actual, (
            f"Module docstring says '{m.group(1)} tools' but {actual} @mcp.tool() "
            "decorators are defined — update the docstring when adding/removing tools"
        )


class TestTaskEviction:
    """_tasks dict is capped at _TASK_CAP; oldest completed tasks evicted first."""

    def test_evict_completed_before_running(self):
        from unittest.mock import MagicMock, patch
        from tommy.mcp_server import _evict_tasks, _tasks, _TASK_CAP

        # Clear and populate with fake tasks
        _tasks.clear()

        def _fake_task(status):
            t = MagicMock()
            t.status.return_value = status
            return t

        # Fill with (cap) completed tasks
        for i in range(_TASK_CAP):
            _tasks[f"done-{i}"] = _fake_task("done")
        # Add one running task that would push us over cap
        _tasks["running-0"] = _fake_task("running")

        assert len(_tasks) == _TASK_CAP + 1
        _evict_tasks()

        # Should now be at cap
        assert len(_tasks) == _TASK_CAP
        # The running task must NOT have been evicted (completed go first)
        assert "running-0" in _tasks
        # The oldest completed task (done-0) should be evicted first
        assert "done-0" not in _tasks
        _tasks.clear()

    def test_cap_enforced_on_overflow(self):
        from unittest.mock import MagicMock
        from tommy.mcp_server import _evict_tasks, _tasks, _TASK_CAP

        _tasks.clear()
        def _fake_done():
            t = MagicMock()
            t.status.return_value = "done"
            return t

        for i in range(_TASK_CAP + 10):
            _tasks[f"t-{i}"] = _fake_done()

        _evict_tasks()
        assert len(_tasks) == _TASK_CAP
        # Newest entries should survive (eviction is oldest-first)
        for i in range(10, _TASK_CAP + 10):
            assert f"t-{i}" in _tasks
        _tasks.clear()
