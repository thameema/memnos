"""
Integration tests for issue #136's dispatch-scoping wiring, at BOTH real
launch call sites: mcp_server.py's tommy_dispatch() and cli.py's
_launch_harness(). Pure subprocess/filesystem tests — no DB, no live memnos
server, no real `claude` binary (see fixtures/scope_capture_harness.py) —
these are the automated acceptance-criteria tests 1-3 from the issue #136
PR description; test_memnos_scope_e2e.py separately covers the same three
scenarios against the REAL `claude` binary + a live memnos server
(skip-by-default, no `claude`/live-server dependency in CI).

Mirrors test_dispatch_stdin_isolation.py's proof strategy deliberately: a
real subprocess (or real in-process call for the MCP path, which never
calls sys.exit) running the REAL, unmodified tommy_dispatch()/
_launch_harness(), against a stand-in "claude" binary that reports exactly
what it observed (argv, env, and the generated files' own content) rather
than a mock that could pass vacuously.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
SCOPE_HARNESS = FIXTURES / "scope_capture_harness.py"
SCOPE_CLI_DRIVER = FIXTURES / "scope_cli_driver.py"

_DRIVER_TIMEOUT = 20.0


def _write_yaml_with_namespace(ws: Path, namespace: str) -> None:
    (ws / "tommy.yaml").write_text(
        f"tommy:\n  version: 1\nmemnos:\n  namespace: {namespace}\n"
    )


def _write_yaml_no_namespace(ws: Path) -> None:
    (ws / "tommy.yaml").write_text("tommy:\n  version: 1\n")


def _write_binding(ws: Path, cache_path: Path, namespace: str = "org:already-bound") -> None:
    """A host_path binding that actually matches `ws` on THIS machine —
    `ws` here is never a git repo in these tests (no remote to key a
    repo-based binding on), so host_path is the only tier that can match a
    plain directory."""
    import tommy.memnos_scope as ms
    cache_path.write_text(json.dumps({
        "bindings": [{
            "key_type": "host_path", "host_id": ms._machine_id(),
            "key": os.path.realpath(str(ws)), "namespace": namespace,
        }]
    }))


# ---------------------------------------------------------------------------
# MCP path: mcp_server.py's tommy_dispatch() (in-process — never calls
# sys.exit, unlike the CLI path below).
# ---------------------------------------------------------------------------


class TestMcpDispatchScope:
    def _dispatch(self, monkeypatch, tmp_path, *, memnos_token="mnk_test_token_ABC123"):
        import tommy.mcp_server as mcp_server_mod
        from tommy.config import TommyConfig
        from tommy.discovery.harnesses import HarnessSpec

        capture_file = tmp_path / "capture.json"
        monkeypatch.setenv("TOMMY_TEST_SCOPE_CAPTURE_FILE", str(capture_file))

        spec = HarnessSpec(
            name="claude", binary=str(SCOPE_HARNESS),
            launch_template=[str(SCOPE_HARNESS), "--append-system-prompt-file", "{prompt_file}"],
            supports_tools=True, supports_mcp=True,
            description="issue #136 test stand-in", available=True,
        )
        monkeypatch.setattr(mcp_server_mod, "all_harnesses", lambda: {"claude": spec})
        cfg = TommyConfig(harness="claude", skip_permissions=False,
                           memnos_url="http://127.0.0.1:8900", memnos_token=memnos_token)
        monkeypatch.setattr(mcp_server_mod, "_cfg", cfg, raising=False)
        monkeypatch.setattr(mcp_server_mod, "_active_project", None, raising=False)

        result = mcp_server_mod.tommy_dispatch(
            task="issue-136 integration test", harness="claude",
            workspace=str(tmp_path), async_run=False, inject_memory=False,
        )
        assert result.get("status") == "done", result
        assert capture_file.exists(), "stand-in harness never ran"
        captured = json.loads(capture_file.read_text())
        return result, captured

    def test_no_binding_explicit_namespace_scopes(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MEMNOS_NS", "")
        monkeypatch.delenv("MEMNOS_NS", raising=False)
        import tommy.memnos_scope as ms
        monkeypatch.setattr(ms, "_BINDINGS_CACHE", tmp_path / "no_such_cache.json")
        monkeypatch.setattr(ms, "_NS_OVERRIDES", tmp_path / "no_such_overrides.json")
        monkeypatch.setattr(ms, "memnos_binary", lambda: "/usr/bin/true")
        _write_yaml_with_namespace(tmp_path, "test:issue-136-scoped")

        result, captured = self._dispatch(monkeypatch, tmp_path)

        assert result.get("memnos_scope") == {"active": True, "namespace": "test:issue-136-scoped"}
        assert "--setting-sources" in captured["argv"]
        idx = captured["argv"].index("--setting-sources")
        assert captured["argv"][idx + 1] == "project,local"

        assert captured["env"].get("MEMNOS_NS") == "test:issue-136-scoped"
        assert captured["env"].get("MEMNOS_TOKEN") == "mnk_test_token_ABC123"
        assert captured["env"].get("MEMNOS_URL") == "http://127.0.0.1:8900"

        assert captured["mcp_json_text"] is not None, ".mcp.json was not generated before launch"
        assert captured["settings_local_text"] is not None
        assert "${MEMNOS_TOKEN}" in captured["mcp_json_text"]
        assert "mnk_test_token_ABC123" not in captured["mcp_json_text"], (
            "real token leaked as a literal into the generated .mcp.json"
        )
        assert "mnk_test_token_ABC123" not in captured["settings_local_text"], (
            "real token leaked as a literal into the generated settings.local.json"
        )
        assert "mcp__memnos" in captured["settings_local_text"]

        # Cleanup: by the time async_run=False returns, the drain thread has
        # already joined (mcp_server.py's tommy_dispatch does this
        # explicitly) — the scoping files must be gone.
        assert not (tmp_path / ".mcp.json").exists(), "scoping .mcp.json was not cleaned up"
        assert not (tmp_path / ".claude" / "settings.local.json").exists(), (
            "scoping settings.local.json was not cleaned up"
        )

    def test_existing_binding_does_not_scope(self, tmp_path, monkeypatch):
        import tommy.memnos_scope as ms
        cache = tmp_path / "bindings_cache.json"
        monkeypatch.setattr(ms, "_BINDINGS_CACHE", cache)
        monkeypatch.setattr(ms, "_NS_OVERRIDES", tmp_path / "no_such_overrides.json")
        monkeypatch.setattr(ms, "memnos_binary", lambda: "/usr/bin/true")
        _write_yaml_with_namespace(tmp_path, "test:issue-136-scoped")
        _write_binding(tmp_path, cache)

        result, captured = self._dispatch(monkeypatch, tmp_path)

        assert "memnos_scope" not in result
        assert "--setting-sources" not in captured["argv"], (
            "an existing binding must leave the ambient USER-scope hooks "
            "untouched — adding --setting-sources here would silently strip "
            "them even though this is exactly the case that must be a no-op"
        )
        assert captured["mcp_json_text"] is None
        assert captured["settings_local_text"] is None
        assert "MEMNOS_NS" not in captured["env"] or captured["env"].get("MEMNOS_NS") != "test:issue-136-scoped"

    def test_no_explicit_namespace_does_not_scope(self, tmp_path, monkeypatch):
        import tommy.memnos_scope as ms
        monkeypatch.setattr(ms, "_BINDINGS_CACHE", tmp_path / "no_such_cache.json")
        monkeypatch.setattr(ms, "_NS_OVERRIDES", tmp_path / "no_such_overrides.json")
        monkeypatch.setattr(ms, "memnos_binary", lambda: "/usr/bin/true")
        _write_yaml_no_namespace(tmp_path)

        result, captured = self._dispatch(monkeypatch, tmp_path)

        assert "memnos_scope" not in result
        assert "--setting-sources" not in captured["argv"]
        assert captured["mcp_json_text"] is None
        assert captured["settings_local_text"] is None

    def test_memnos_binary_missing_degrades_to_unscoped(self, tmp_path, monkeypatch):
        import tommy.mcp_server as mcp_server_mod
        import tommy.memnos_scope as ms
        monkeypatch.setattr(ms, "_BINDINGS_CACHE", tmp_path / "no_such_cache.json")
        monkeypatch.setattr(ms, "_NS_OVERRIDES", tmp_path / "no_such_overrides.json")
        # mcp_server.py does `from .memnos_scope import ... memnos_binary` —
        # that binds the name into mcp_server's OWN module namespace, so the
        # call site must be patched there, not on tommy.memnos_scope itself.
        monkeypatch.setattr(mcp_server_mod, "memnos_binary", lambda: None)
        _write_yaml_with_namespace(tmp_path, "test:issue-136-scoped")

        result, captured = self._dispatch(monkeypatch, tmp_path)

        assert "memnos_scope" not in result
        assert "--setting-sources" not in captured["argv"]
        assert captured["mcp_json_text"] is None


# ---------------------------------------------------------------------------
# Interactive CLI path: cli.py's _launch_harness() — must run as a real
# subprocess (it calls sys.exit()).
# ---------------------------------------------------------------------------


class TestCliLaunchScope:
    def _run(self, tmp_path, capture_file, *, memnos_token="mnk_test_token_XYZ789"):
        env = {
            **os.environ,
            "TOMMY_TEST_HARNESS_SCRIPT": str(SCOPE_HARNESS),
            "TOMMY_TEST_SCOPE_CAPTURE_FILE": str(capture_file),
            "TOMMY_TEST_MEMNOS_TOKEN": memnos_token,
        }
        env.pop("MEMNOS_NS", None)
        proc = subprocess.run(
            [sys.executable, str(SCOPE_CLI_DRIVER)],
            cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=_DRIVER_TIMEOUT,
        )
        assert proc.returncode == 0, f"driver failed: {proc.stdout}\n{proc.stderr}"
        assert capture_file.exists(), "stand-in harness never ran"
        return json.loads(capture_file.read_text())

    def test_no_binding_explicit_namespace_scopes(self, tmp_path):
        capture_file = tmp_path / "capture.json"
        _write_yaml_with_namespace(tmp_path, "test:issue-136-cli-scoped")

        captured = self._run(tmp_path, capture_file)

        assert "--setting-sources" in captured["argv"]
        idx = captured["argv"].index("--setting-sources")
        assert captured["argv"][idx + 1] == "project,local"
        assert captured["env"].get("MEMNOS_NS") == "test:issue-136-cli-scoped"
        assert captured["env"].get("MEMNOS_TOKEN") == "mnk_test_token_XYZ789"
        assert captured["mcp_json_text"] is not None
        assert captured["settings_local_text"] is not None
        assert "mnk_test_token_XYZ789" not in captured["mcp_json_text"]
        assert "mnk_test_token_XYZ789" not in captured["settings_local_text"]

        assert not (tmp_path / ".mcp.json").exists(), "scoping .mcp.json was not cleaned up"
        assert not (tmp_path / ".claude" / "settings.local.json").exists()

    def test_existing_binding_does_not_scope(self, tmp_path):
        capture_file = tmp_path / "capture.json"
        cache = tmp_path / "bindings_cache.json"
        _write_yaml_with_namespace(tmp_path, "test:issue-136-cli-scoped")
        _write_binding(tmp_path, cache)

        import tommy.memnos_scope as ms
        # Route the driver subprocess at this cache file via env, mirroring
        # how the module resolves it at import time from $HOME — simplest
        # sandboxed way to prove this from a real subprocess is to point
        # $HOME's ~/.memnos at a throwaway dir containing this cache file.
        fake_home = tmp_path / "fakehome"
        (fake_home / ".memnos").mkdir(parents=True)
        (fake_home / ".memnos" / "bindings_cache.json").write_text(cache.read_text())

        env = {
            **os.environ, "HOME": str(fake_home),
            "TOMMY_TEST_HARNESS_SCRIPT": str(SCOPE_HARNESS),
            "TOMMY_TEST_SCOPE_CAPTURE_FILE": str(capture_file),
        }
        env.pop("MEMNOS_NS", None)
        proc = subprocess.run(
            [sys.executable, str(SCOPE_CLI_DRIVER)],
            cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=_DRIVER_TIMEOUT,
        )
        assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
        captured = json.loads(capture_file.read_text())

        assert "--setting-sources" not in captured["argv"]
        assert captured["mcp_json_text"] is None
        assert captured["settings_local_text"] is None

    def test_no_explicit_namespace_does_not_scope(self, tmp_path):
        capture_file = tmp_path / "capture.json"
        _write_yaml_no_namespace(tmp_path)

        captured = self._run(tmp_path, capture_file)

        assert "--setting-sources" not in captured["argv"]
        assert captured["mcp_json_text"] is None
        assert captured["settings_local_text"] is None
