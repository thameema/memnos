"""
Integration tests for issue #136's dispatch-scoping wiring, at BOTH real
launch call sites: mcp_server.py's tommy_dispatch() and cli.py's
_launch_harness(). Pure subprocess/filesystem tests — no DB, no live memnos
server, no real `claude` binary (see fixtures/scope_capture_harness.py) —
these are the automated acceptance-criteria tests 1-3 from the issue #136
PR description.

There is deliberately NO automated test file that shells out to a REAL
`claude` binary + a live memnos server for this issue: the two empirical
claims the design depends on (implicit print-mode dispatch actually loads
and can call a project-scoped .mcp.json server, and `--setting-sources
project,local` actually excludes user-scope ~/.claude/settings.json hooks
while still loading project/local-scope ones) were verified by hand against
a real `claude` 2.1.240 binary + a live memnos server — see the issue #136
PR description for the exact commands, marker-file results, and
`claude mcp get memnos` output. That verification is intentionally NOT
wired into pytest: it depends on a real, authenticated `claude` install
(unavailable in CI and on most other dev machines) and, for the
--setting-sources check, on temporarily editing this MACHINE's own
~/.claude/settings.json (backed up and restored byte-identical) — not a
shape that belongs in an automated suite. If Claude Code's own CLI
contract around print-mode MCP approval or --setting-sources ever changes,
these two claims would need re-verifying by hand again the same way, not by
a stale green test that never touched the real binary.

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
        import tommy.mcp_server as mcp_server_mod
        import tommy.memnos_scope as ms
        monkeypatch.setattr(ms, "_BINDINGS_CACHE", tmp_path / "no_such_cache.json")
        monkeypatch.setattr(ms, "_NS_OVERRIDES", tmp_path / "no_such_overrides.json")
        # generate_scoping_files() persists a small reference-count/lock/
        # snapshot state file per workspace under ~/.memnos/tommy_scope/
        # (see memnos_scope.py's "Concurrency safety" docstring section) —
        # this test is the one MCP-path case that actually activates
        # scoping (in-process, so this runs against the REAL $HOME unless
        # isolated here, same reasoning as _BINDINGS_CACHE/_NS_OVERRIDES
        # above).
        monkeypatch.setattr(ms, "_SCOPE_STATE_DIR", tmp_path / "_scope_state")
        # mcp_server.py does `from .memnos_scope import ... memnos_binary` — that
        # binds the name into mcp_server's OWN module namespace, so the call site
        # must be patched there, not on tommy.memnos_scope itself (see
        # test_memnos_binary_missing_degrades_to_unscoped's comment below for the
        # same point). Patching `ms.memnos_binary` here was a bug that happened to
        # pass on any machine with a real `memnos` on PATH (shutil.which() then
        # succeeds anyway) but fails on a clean CI runner with no `memnos`
        # installed — caught by CI on this exact test, issue #136.
        monkeypatch.setattr(mcp_server_mod, "memnos_binary", lambda: "/usr/bin/true")
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
        import tommy.mcp_server as mcp_server_mod
        import tommy.memnos_scope as ms
        cache = tmp_path / "bindings_cache.json"
        monkeypatch.setattr(ms, "_BINDINGS_CACHE", cache)
        monkeypatch.setattr(ms, "_NS_OVERRIDES", tmp_path / "no_such_overrides.json")
        # See test_no_binding_explicit_namespace_scopes above: must patch the
        # name as bound into mcp_server_mod's own namespace, not ms's.
        monkeypatch.setattr(mcp_server_mod, "memnos_binary", lambda: "/usr/bin/true")
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
        import tommy.mcp_server as mcp_server_mod
        import tommy.memnos_scope as ms
        monkeypatch.setattr(ms, "_BINDINGS_CACHE", tmp_path / "no_such_cache.json")
        monkeypatch.setattr(ms, "_NS_OVERRIDES", tmp_path / "no_such_overrides.json")
        # See test_no_binding_explicit_namespace_scopes above: must patch the
        # name as bound into mcp_server_mod's own namespace, not ms's.
        monkeypatch.setattr(mcp_server_mod, "memnos_binary", lambda: "/usr/bin/true")
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


def _fake_memnos_bin_dir(tmp_path: Path) -> Path:
    """A scratch dir on PATH with a stub `memnos` executable — cli.py's
    _launch_harness() checks memnos_binary() (shutil.which("memnos")) as an
    independent environment fact before scoping a dispatch (see
    memnos_scope.py's memnos_binary() docstring); it must resolve to
    SOMETHING for a positive "scopes" test to be a real test of the
    scoping logic rather than of whatever happens to be on the test
    runner's own PATH (memnos IS on PATH on a dev laptop that has it
    installed, but is NOT on a clean CI runner — this exact gap is what
    made test_no_binding_explicit_namespace_scopes pass locally and fail
    in CI, issue #136)."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "memnos"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)
    return bin_dir


class TestCliLaunchScope:
    def _run(self, tmp_path, capture_file, *, memnos_token="mnk_test_token_XYZ789",
             fake_memnos_on_path=False, isolate_home=False):
        env = {
            **os.environ,
            "TOMMY_TEST_HARNESS_SCRIPT": str(SCOPE_HARNESS),
            "TOMMY_TEST_SCOPE_CAPTURE_FILE": str(capture_file),
            "TOMMY_TEST_MEMNOS_TOKEN": memnos_token,
        }
        env.pop("MEMNOS_NS", None)
        if fake_memnos_on_path:
            bin_dir = _fake_memnos_bin_dir(tmp_path)
            env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
        if isolate_home:
            # generate_scoping_files() now persists a small reference-count/
            # lock/snapshot state file per workspace under
            # ~/.memnos/tommy_scope/ (see memnos_scope.py's "Concurrency
            # safety" docstring section) — same directory family as
            # bindings_cache.json, which test_existing_binding_does_not_scope
            # below already isolates via a fake $HOME for exactly this
            # reason. A scoping-POSITIVE test (this one) actually exercises
            # that write path, unlike the "does not scope" tests, so it must
            # not touch whoever's real $HOME runs this suite either.
            fake_home = tmp_path / "fakehome"
            (fake_home / ".memnos").mkdir(parents=True, exist_ok=True)
            env["HOME"] = str(fake_home)
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

        captured = self._run(tmp_path, capture_file, fake_memnos_on_path=True, isolate_home=True)

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
