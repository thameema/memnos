"""
Unit tests for tommy/memnos_scope.py (issue #136).

Covers:
  - has_existing_binding() tiers (repo / host_repo / host_path / legacy),
    plus a parity fuzz test against the REAL nsresolve.py (repo-root-only
    import — see _import_nsresolve() below) so a future drift in
    nsresolve's bindings_cache.json shape becomes a red test here, not a
    silent divergence (see memnos_scope.py's module docstring).
  - explicit_yaml_namespace() / should_scope_dispatch()'s literal reading
    of tommy.yaml's memnos.namespace (never widened to
    effective_config.py's always-has-a-value resolution — see its
    docstring for why that would be wrong here).
  - generate_scoping_files()/ScopingFiles.cleanup(): placeholder-only
    content (never a literal secret), merge-not-clobber of a pre-existing
    file, and byte-identical restore on cleanup.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import tommy.memnos_scope as ms

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _import_nsresolve():
    """Import the real repo-root nsresolve.py directly. This is a
    dev/test-environment-only import (repo-root is not on tommy-orchestrator's
    own dependency closure at runtime — see memnos_scope.py's module
    docstring) used ONLY to prove parity, never at runtime."""
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    import nsresolve  # type: ignore
    return nsresolve


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _init_repo(path: Path, remote: str | None = None) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], path)
    _git(["config", "user.email", "test@example.com"], path)
    _git(["config", "user.name", "Test"], path)
    (path / "README.md").write_text("x")
    _git(["add", "."], path)
    _git(["commit", "-q", "-m", "init"], path)
    if remote:
        _git(["remote", "add", "origin", remote], path)


# ---------------------------------------------------------------------------
# has_existing_binding() — tiers 2-4, plus real-nsresolve parity
# ---------------------------------------------------------------------------


class TestHasExistingBinding:
    def test_no_cache_no_overrides_returns_false(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ms, "_BINDINGS_CACHE", tmp_path / "bindings_cache.json")
        monkeypatch.setattr(ms, "_NS_OVERRIDES", tmp_path / "ns_overrides.json")
        ws = tmp_path / "ws"
        _init_repo(ws, remote="git@github.com:org/repo.git")
        assert ms.has_existing_binding(ws) is False

    def test_repo_binding_tier(self, tmp_path, monkeypatch):
        cache = tmp_path / "bindings_cache.json"
        monkeypatch.setattr(ms, "_BINDINGS_CACHE", cache)
        monkeypatch.setattr(ms, "_NS_OVERRIDES", tmp_path / "ns_overrides.json")
        ws = tmp_path / "ws"
        _init_repo(ws, remote="https://github.com/org/repo.git")
        cache.write_text(json.dumps({
            "bindings": [{"key_type": "repo", "key": "github.com/org/repo", "namespace": "org:eng"}]
        }))
        assert ms.has_existing_binding(ws) is True

    def test_host_repo_binding_tier(self, tmp_path, monkeypatch):
        cache = tmp_path / "bindings_cache.json"
        monkeypatch.setattr(ms, "_BINDINGS_CACHE", cache)
        monkeypatch.setattr(ms, "_NS_OVERRIDES", tmp_path / "ns_overrides.json")
        ws = tmp_path / "ws"
        _init_repo(ws, remote="git@github.com:org/repo2.git")
        mid = ms._machine_id()
        cache.write_text(json.dumps({
            "bindings": [{"key_type": "host_repo", "host_id": mid,
                          "key": "github.com/org/repo2", "namespace": "org:eng"}]
        }))
        assert ms.has_existing_binding(ws) is True

    def test_host_repo_binding_wrong_host_does_not_count(self, tmp_path, monkeypatch):
        cache = tmp_path / "bindings_cache.json"
        monkeypatch.setattr(ms, "_BINDINGS_CACHE", cache)
        monkeypatch.setattr(ms, "_NS_OVERRIDES", tmp_path / "ns_overrides.json")
        ws = tmp_path / "ws"
        _init_repo(ws, remote="git@github.com:org/repo3.git")
        cache.write_text(json.dumps({
            "bindings": [{"key_type": "host_repo", "host_id": "some-other-machine",
                          "key": "github.com/org/repo3", "namespace": "org:eng"}]
        }))
        assert ms.has_existing_binding(ws) is False

    def test_host_path_binding_tier_no_remote(self, tmp_path, monkeypatch):
        """The exact issue #136 scenario: a repo with NO 'origin' remote
        (e.g. named 'github' instead — see nsresolve.repo_key()'s docstring)
        falls through repo-keyed tiers entirely; only a host_path binding on
        the literal directory can match."""
        cache = tmp_path / "bindings_cache.json"
        monkeypatch.setattr(ms, "_BINDINGS_CACHE", cache)
        monkeypatch.setattr(ms, "_NS_OVERRIDES", tmp_path / "ns_overrides.json")
        ws = tmp_path / "ws"
        _init_repo(ws, remote=None)
        _git(["remote", "add", "github", "git@github.com:org/repo4.git"], ws)
        mid = ms._machine_id()
        import os
        cache.write_text(json.dumps({
            "bindings": [{"key_type": "host_path", "host_id": mid,
                          "key": os.path.realpath(str(ws)), "namespace": "org:eng"}]
        }))
        assert ms.has_existing_binding(ws) is True

    def test_legacy_ns_overrides_tier(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ms, "_BINDINGS_CACHE", tmp_path / "bindings_cache.json")
        overrides = tmp_path / "ns_overrides.json"
        monkeypatch.setattr(ms, "_NS_OVERRIDES", overrides)
        ws = tmp_path / "ws"
        ws.mkdir()  # not even a git repo — legacy override is path-keyed
        overrides.write_text(json.dumps({str(ws): "user:someone"}))
        assert ms.has_existing_binding(ws) is True

    def test_env_and_default_tiers_are_not_bindings(self, tmp_path, monkeypatch):
        """MEMNOS_NS env being set must NOT count as an existing binding —
        that's tier 5, deliberately excluded (see has_existing_binding's
        docstring: this is one of the exact 'unbound' cases the fix targets)."""
        monkeypatch.setattr(ms, "_BINDINGS_CACHE", tmp_path / "bindings_cache.json")
        monkeypatch.setattr(ms, "_NS_OVERRIDES", tmp_path / "ns_overrides.json")
        monkeypatch.setenv("MEMNOS_NS", "user:someone")
        ws = tmp_path / "ws"
        _init_repo(ws, remote="git@github.com:org/repo5.git")
        assert ms.has_existing_binding(ws) is False

    def test_parity_with_real_nsresolve_across_scenarios(self, tmp_path, monkeypatch):
        """The load-bearing regression guard: has_existing_binding() must
        agree with the REAL nsresolve.resolve_with_source() on whether a
        binding exists, for a battery of synthetic cache states — proving
        this module's replication hasn't drifted from its canonical source.
        See memnos_scope.py's module docstring."""
        nsresolve = _import_nsresolve()

        cache = tmp_path / "bindings_cache.json"
        overrides = tmp_path / "ns_overrides.json"
        mid_dir = tmp_path / "midhome"
        mid_dir.mkdir()

        monkeypatch.setattr(ms, "_BINDINGS_CACHE", cache)
        monkeypatch.setattr(ms, "_NS_OVERRIDES", overrides)
        monkeypatch.setattr(nsresolve, "_CACHE", str(cache))
        monkeypatch.setattr(nsresolve, "_OVR", str(overrides))
        monkeypatch.setattr(nsresolve, "_DIR", str(mid_dir))
        monkeypatch.setattr(nsresolve, "_MID", str(mid_dir / "machine_id"))
        monkeypatch.delenv("MEMNOS_NS", raising=False)

        mid = ms._machine_id()
        assert mid == nsresolve.machine_id(), "machine_id derivation itself diverged"

        ws_repo_bound = tmp_path / "repo_bound"
        _init_repo(ws_repo_bound, remote="git@github.com:org/a.git")
        ws_host_repo_bound = tmp_path / "host_repo_bound"
        _init_repo(ws_host_repo_bound, remote="https://github.com/org/b.git")
        ws_host_path_bound = tmp_path / "host_path_bound"
        ws_host_path_bound.mkdir()
        ws_legacy_bound = tmp_path / "legacy_bound"
        ws_legacy_bound.mkdir()
        ws_unbound = tmp_path / "unbound"
        _init_repo(ws_unbound, remote="git@github.com:org/c.git")

        import os
        cache.write_text(json.dumps({"bindings": [
            {"key_type": "repo", "key": "github.com/org/a", "namespace": "n1"},
            {"key_type": "host_repo", "host_id": mid, "key": "github.com/org/b", "namespace": "n2"},
            {"key_type": "host_path", "host_id": mid,
             "key": os.path.realpath(str(ws_host_path_bound)), "namespace": "n3"},
        ]}))
        overrides.write_text(json.dumps({str(ws_legacy_bound): "n4"}))

        for ws in (ws_repo_bound, ws_host_repo_bound, ws_host_path_bound, ws_legacy_bound, ws_unbound):
            ns_ns, ns_source = nsresolve.resolve_with_source({"cwd": str(ws)})
            expected = ns_source in nsresolve.BOUND_SOURCES and ns_source != "explicit"
            actual = ms.has_existing_binding(ws)
            assert actual == expected, (
                f"{ws.name}: nsresolve source={ns_source!r} (bound={expected}) "
                f"but has_existing_binding()={actual}"
            )


# ---------------------------------------------------------------------------
# explicit_yaml_namespace() / should_scope_dispatch()
# ---------------------------------------------------------------------------


class TestExplicitYamlNamespace:
    def test_namespace_set_returns_it(self, tmp_path):
        (tmp_path / "tommy.yaml").write_text(
            "tommy:\n  version: 1\nmemnos:\n  namespace: org:demo:eng\n"
        )
        assert ms.explicit_yaml_namespace(tmp_path) == "org:demo:eng"

    def test_no_tommy_yaml_returns_none(self, tmp_path):
        assert ms.explicit_yaml_namespace(tmp_path) is None

    def test_tommy_yaml_without_namespace_returns_none(self, tmp_path):
        (tmp_path / "tommy.yaml").write_text("tommy:\n  version: 1\n")
        assert ms.explicit_yaml_namespace(tmp_path) is None

    def test_broken_tommy_yaml_degrades_to_none(self, tmp_path):
        (tmp_path / "tommy.yaml").write_text("not: valid: yaml: [[[")
        assert ms.explicit_yaml_namespace(tmp_path) is None

    def test_never_widened_to_effective_config_default(self, tmp_path):
        """Regression guard for the specific mistake the design explicitly
        rejects: effective_config.resolve_effective_config().value("namespace")
        ALWAYS returns a value (falls back to TommyConfig.default_ns) — using
        it here would invent scope for every dispatch. explicit_yaml_namespace
        must return None when tommy.yaml doesn't set memnos.namespace, even
        though the "effective" resolved namespace is never actually empty."""
        (tmp_path / "tommy.yaml").write_text("tommy:\n  version: 1\n")
        from tommy.effective_config import resolve_effective_config
        effective = resolve_effective_config(project_root=tmp_path)
        assert effective.value("namespace")  # sanity: always truthy
        assert ms.explicit_yaml_namespace(tmp_path) is None


class TestShouldScopeDispatch:
    def test_no_yaml_no_scope(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ms, "_BINDINGS_CACHE", tmp_path / "bindings_cache.json")
        monkeypatch.setattr(ms, "_NS_OVERRIDES", tmp_path / "ns_overrides.json")
        assert ms.should_scope_dispatch(tmp_path) == (False, None)

    def test_yaml_namespace_no_binding_scopes(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ms, "_BINDINGS_CACHE", tmp_path / "bindings_cache.json")
        monkeypatch.setattr(ms, "_NS_OVERRIDES", tmp_path / "ns_overrides.json")
        (tmp_path / "tommy.yaml").write_text(
            "tommy:\n  version: 1\nmemnos:\n  namespace: test:issue-136\n"
        )
        assert ms.should_scope_dispatch(tmp_path) == (True, "test:issue-136")

    def test_yaml_namespace_with_binding_does_not_scope(self, tmp_path, monkeypatch):
        cache = tmp_path / "bindings_cache.json"
        overrides = tmp_path / "ns_overrides.json"
        monkeypatch.setattr(ms, "_BINDINGS_CACHE", cache)
        monkeypatch.setattr(ms, "_NS_OVERRIDES", overrides)
        (tmp_path / "tommy.yaml").write_text(
            "tommy:\n  version: 1\nmemnos:\n  namespace: test:issue-136\n"
        )
        cache.write_text(json.dumps({"bindings": []}))
        overrides.write_text(json.dumps({str(tmp_path): "org:already-bound"}))
        assert ms.should_scope_dispatch(tmp_path) == (False, None)


# ---------------------------------------------------------------------------
# generate_scoping_files() / ScopingFiles.cleanup()
# ---------------------------------------------------------------------------


class TestGenerateScopingFiles:
    def test_fresh_workspace_writes_placeholders_only(self, tmp_path):
        scoping = ms.generate_scoping_files(tmp_path)
        mcp_text = (tmp_path / ".mcp.json").read_text()
        settings_text = (tmp_path / ".claude" / "settings.local.json").read_text()

        assert "${MEMNOS_URL}" in mcp_text
        assert "${MEMNOS_TOKEN}" in mcp_text
        assert "${MEMNOS_NS}" in mcp_text
        assert "${MEMNOS_TOKEN}" in settings_text
        assert "${MEMNOS_NS}" in settings_text

        mcp_data = json.loads(mcp_text)
        assert mcp_data["mcpServers"]["memnos"]["command"] == "memnos"
        assert mcp_data["mcpServers"]["memnos"]["args"] == ["mcp"]

        settings_data = json.loads(settings_text)
        assert "mcp__memnos" in settings_data["permissions"]["allow"]
        assert "memnos hook recall" in json.dumps(settings_data["hooks"]["UserPromptSubmit"])
        assert "memnos hook remember" in json.dumps(settings_data["hooks"]["Stop"])

        scoping.cleanup()
        assert not (tmp_path / ".mcp.json").exists()
        assert not (tmp_path / ".claude" / "settings.local.json").exists()

    def test_generated_files_carry_only_placeholders_never_a_value(self, tmp_path):
        """generate_scoping_files() takes no token/url/namespace argument at
        all — it CANNOT write a literal secret, by construction, since it
        has no value to write. This test locks in that shape (every env
        reference in both files is the literal string "${...}", nothing
        else) so a future refactor that starts threading a real value into
        this function is caught here. The actual secret-hygiene proof (a
        REAL token present in cfg/env not leaking into the written files)
        is test_dispatch_scope_integration.py::test_no_binding_explicit_namespace_scopes,
        which is the only place a real token is ever in scope."""
        scoping = ms.generate_scoping_files(tmp_path)
        mcp_data = json.loads((tmp_path / ".mcp.json").read_text())
        settings_text = (tmp_path / ".claude" / "settings.local.json").read_text()

        mcp_env = mcp_data["mcpServers"]["memnos"]["env"]
        for value in mcp_env.values():
            assert value.startswith("${") and value.endswith("}"), mcp_env

        for var in ("MEMNOS_URL", "MEMNOS_TOKEN", "MEMNOS_NS"):
            assert f"${{{var}}}" in settings_text
        scoping.cleanup()

    def test_merges_into_pre_existing_mcp_json_and_restores_on_cleanup(self, tmp_path):
        original = {"mcpServers": {"other_server": {"command": "other", "args": []}}}
        original_text = json.dumps(original, indent=2) + "\n"
        (tmp_path / ".mcp.json").write_text(original_text)

        scoping = ms.generate_scoping_files(tmp_path)
        merged = json.loads((tmp_path / ".mcp.json").read_text())
        assert "other_server" in merged["mcpServers"], "pre-existing server must survive the merge"
        assert "memnos" in merged["mcpServers"]

        scoping.cleanup()
        assert (tmp_path / ".mcp.json").read_text() == original_text, (
            "cleanup must restore byte-identical content, not just 'a file exists'"
        )

    def test_merges_into_pre_existing_settings_local_and_restores_on_cleanup(self, tmp_path):
        settings_dir = tmp_path / ".claude"
        settings_dir.mkdir()
        original = {
            "permissions": {"allow": ["Bash(git *)"]},
            "hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "echo other-hook"}]}]},
        }
        original_text = json.dumps(original, indent=2) + "\n"
        (settings_dir / "settings.local.json").write_text(original_text)

        scoping = ms.generate_scoping_files(tmp_path)
        merged = json.loads((settings_dir / "settings.local.json").read_text())
        assert "Bash(git *)" in merged["permissions"]["allow"], "pre-existing permission must survive"
        assert "mcp__memnos" in merged["permissions"]["allow"]
        assert any("echo other-hook" in json.dumps(g) for g in merged["hooks"]["UserPromptSubmit"]), (
            "pre-existing hook group must survive the merge"
        )

        scoping.cleanup()
        assert (settings_dir / "settings.local.json").read_text() == original_text

    def test_regenerating_is_idempotent_no_duplicate_hook_groups(self, tmp_path):
        ms.generate_scoping_files(tmp_path)
        ms.generate_scoping_files(tmp_path)  # simulate a second dispatch reusing the workspace
        settings_text = (tmp_path / ".claude" / "settings.local.json").read_text()
        data = json.loads(settings_text)
        assert len(data["hooks"]["UserPromptSubmit"]) == 1
        assert len(data["hooks"]["Stop"]) == 1
        allow = data["permissions"]["allow"]
        assert allow.count("mcp__memnos") == 1

    def test_cleanup_is_idempotent(self, tmp_path):
        scoping = ms.generate_scoping_files(tmp_path)
        scoping.cleanup()
        scoping.cleanup()  # must not raise
        assert not (tmp_path / ".mcp.json").exists()
