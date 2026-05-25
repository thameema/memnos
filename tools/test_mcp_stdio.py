"""
engram MCP stdio transport test suite.

What this suite covers
----------------------
- Process start: engram-mcp-stdio spawns without crashing
- JSON-RPC initialize handshake: response includes capabilities.tools
- tools/list: response includes at least memory_write and memory_search
- tools/call memory_write: returns a valid id and no JSON-RPC error
- tools/call memory_search: returns results array or no-match string

Transport protocol
------------------
The MCP stdio transport speaks line-delimited JSON-RPC 2.0 over stdin/stdout.
Each message is a single JSON object terminated by a newline.

Binary
------
{_REPO_ROOT}/.venv/bin/engram-mcp-stdio

Config
------
{_REPO_ROOT}/engram.yaml

Required environment variables
-------------------------------
ARCADEDB_PASSWORD=engram-dev-password
ENGRAM_API_KEY=engram-local-dev-key
ENGRAM_VAULT_KEY=dev-key-for-local-testing-only
ENGRAM_CONFIG={_REPO_ROOT}/engram.yaml

Run
---
python -m pytest tools/test_mcp_stdio.py -v
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BINARY = str(Path(_REPO_ROOT) / ".venv/bin/engram-mcp-stdio")
_CONFIG = str(Path(_REPO_ROOT) / "engram.yaml")
_STARTUP_TIMEOUT_S = 45      # seconds to wait for the process to be ready
_RPC_TIMEOUT_S = 30          # seconds to wait for a single RPC response
_TEST_NS = f"test:mcp-stdio:{uuid.uuid4().hex[:8]}"

_ENV = {
    **os.environ,
    "ARCADEDB_PASSWORD": os.environ.get("ARCADEDB_PASSWORD", "engram-dev-password"),
    "ENGRAM_API_KEY": os.environ.get("ENGRAM_API_KEY", "engram-local-dev-key"),
    "ENGRAM_VAULT_KEY": os.environ.get("ENGRAM_VAULT_KEY", "dev-key-for-local-testing-only"),
    "ENGRAM_CONFIG": _CONFIG,
    "ENGRAM_LOG_LEVEL": "WARNING",   # reduce stderr noise during tests
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _send(proc: subprocess.Popen, msg: dict[str, Any]) -> None:
    """Write one JSON-RPC message to the process stdin."""
    line = json.dumps(msg, separators=(",", ":")) + "\n"
    proc.stdin.write(line.encode())
    proc.stdin.flush()


def _recv(proc: subprocess.Popen, timeout: float = _RPC_TIMEOUT_S) -> dict[str, Any]:
    """Read one JSON-RPC response from stdout, with a deadline."""
    import select as _select
    deadline = time.monotonic() + timeout
    buf = b""
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"MCP process exited unexpectedly with code {proc.returncode}"
            )
        remaining = max(0.1, deadline - time.monotonic())
        ready, _, _ = _select.select([proc.stdout], [], [], min(0.1, remaining))
        if ready:
            chunk = proc.stdout.readline()
        else:
            chunk = b""
        if chunk:
            buf += chunk
            try:
                return json.loads(buf.decode("utf-8"))
            except json.JSONDecodeError:
                # Partial line — keep buffering
                continue
    raise TimeoutError(
        f"No JSON-RPC response received within {timeout}s. Buffer so far: {buf!r}"
    )


def _rpc(proc: subprocess.Popen, method: str, params: dict | None = None, msg_id: int = 1) -> dict[str, Any]:
    """Send a JSON-RPC 2.0 request and return the parsed response."""
    msg: dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": msg_id,
        "method": method,
    }
    if params is not None:
        msg["params"] = params
    _send(proc, msg)
    return _recv(proc)


# ---------------------------------------------------------------------------
# Session-scoped fixture: one process for all tests in this module
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def mcp_proc():
    """Spawn engram-mcp-stdio, yield the Popen object, kill on teardown."""
    if not os.path.isfile(_BINARY):
        pytest.skip(f"engram-mcp-stdio binary not found at {_BINARY}")

    import select as _sel
    import threading

    proc = subprocess.Popen(
        [_BINARY],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_ENV,
        bufsize=0,
    )

    # Drain stderr in a background thread so it doesn't block the process
    stderr_lines: list[str] = []
    def _drain_stderr() -> None:
        for line in proc.stderr:
            stderr_lines.append(line.decode("utf-8", errors="replace").rstrip())
    _t = threading.Thread(target=_drain_stderr, daemon=True)
    _t.start()

    # Wait until "MCP server ready" appears in stderr or process dies
    deadline = time.monotonic() + _STARTUP_TIMEOUT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            _t.join(timeout=2)
            pytest.fail(
                f"engram-mcp-stdio exited unexpectedly (code {proc.returncode}).\n"
                f"stderr:\n" + "\n".join(stderr_lines[-30:])
            )
        if any("MCP server ready" in l or "stdio transport" in l for l in stderr_lines):
            break
        time.sleep(0.5)
    else:
        # Timed out — check if process is still running
        if proc.poll() is not None:
            pytest.fail(f"engram-mcp-stdio exited (code {proc.returncode}): {chr(10).join(stderr_lines[-20:])}")
        # Process running but never printed ready — proceed anyway

    yield proc

    # Teardown: terminate gracefully, then kill if needed
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
        proc.wait(timeout=3)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestMcpStdioTransport:
    """JSON-RPC 2.0 contract tests over the MCP stdio transport."""

    def test_initialize_returns_capabilities(self, mcp_proc: subprocess.Popen) -> None:
        """The JSON-RPC 'initialize' handshake must return a result with capabilities.tools."""
        resp = _rpc(
            mcp_proc,
            method="initialize",
            params={
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "1.0"},
            },
            msg_id=1,
        )
        assert "error" not in resp, f"initialize returned error: {resp.get('error')}"
        result = resp.get("result", {})
        assert result, f"initialize result is empty: {resp}"

        capabilities = result.get("capabilities", {})
        assert "tools" in capabilities, (
            f"Expected 'tools' key in capabilities, got: {capabilities}"
        )

    def test_tools_list_includes_required_tools(self, mcp_proc: subprocess.Popen) -> None:
        """tools/list must include at least 'memory_write' and 'memory_search'."""
        resp = _rpc(mcp_proc, method="tools/list", msg_id=2)
        assert "error" not in resp, f"tools/list returned error: {resp.get('error')}"
        result = resp.get("result", {})
        tools_list = result.get("tools", [])
        assert isinstance(tools_list, list), (
            f"tools/list result.tools must be a list, got: {type(tools_list)}"
        )
        tool_names = {t.get("name") for t in tools_list}
        required = {"memory_write", "memory_search"}
        missing = required - tool_names
        assert not missing, (
            f"Required tools missing from tools/list: {missing}. "
            f"Available tools: {tool_names}"
        )

    def test_memory_write_succeeds(self, mcp_proc: subprocess.Popen) -> None:
        """tools/call memory_write must return a result with an 'id' field and no error."""
        resp = _rpc(
            mcp_proc,
            method="tools/call",
            params={
                "name": "memory_write",
                "arguments": {
                    "content": "MCP stdio test: engram write round-trip check",
                    "namespace": _TEST_NS,
                    "tags": ["mcp-test"],
                },
            },
            msg_id=3,
        )
        assert "error" not in resp, (
            f"tools/call memory_write returned JSON-RPC error: {resp.get('error')}"
        )
        result = resp.get("result", {})
        # MCP returns content as a list of TextContent items
        content_items = result.get("content", [])
        assert len(content_items) > 0, f"Empty content in memory_write result: {result}"

        # Parse the text payload — should be a JSON dict with an 'id' field
        first_text = content_items[0].get("text", "")
        try:
            payload = json.loads(first_text)
        except json.JSONDecodeError:
            pytest.fail(
                f"memory_write result text is not valid JSON: {first_text!r}"
            )

        assert "id" in payload, (
            f"Expected 'id' in memory_write response payload, got: {payload}"
        )

    def test_memory_search_returns_results(self, mcp_proc: subprocess.Popen) -> None:
        """tools/call memory_search must not error and must return text content."""
        # Allow the write from the previous test to be indexed
        time.sleep(2)

        resp = _rpc(
            mcp_proc,
            method="tools/call",
            params={
                "name": "memory_search",
                "arguments": {
                    "query": "engram write round-trip check",
                    "namespace": _TEST_NS,
                    "top_k": 5,
                },
            },
            msg_id=4,
        )
        assert "error" not in resp, (
            f"tools/call memory_search returned JSON-RPC error: {resp.get('error')}"
        )
        result = resp.get("result", {})
        content_items = result.get("content", [])
        assert len(content_items) > 0, (
            f"memory_search returned no content items: {result}"
        )
        # The response is a formatted string (see handle_memory_search) —
        # it should mention "memories" or "No memories"
        response_text = " ".join(item.get("text", "") for item in content_items)
        assert response_text.strip(), "memory_search returned empty text response"
        # Acceptable outcomes: found memories OR no-match message
        assert "memor" in response_text.lower() or "found" in response_text.lower(), (
            f"Unexpected memory_search response: {response_text[:300]}"
        )
