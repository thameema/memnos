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
import threading
import time
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
    @pytest.fixture(autouse=True)
    def _isolate_scope_state(self, tmp_path, monkeypatch):
        """generate_scoping_files()/ScopingFiles.cleanup() now persist a
        small reference-count/lock/snapshot state file per workspace under
        ~/.memnos/tommy_scope/ (see memnos_scope.py's "Concurrency safety"
        module docstring section) — same local-host-state directory family
        as _BINDINGS_CACHE/_NS_OVERRIDES, which existing tests in this file
        already isolate per-test. Autouse here so every test in this class
        gets a throwaway state dir without having to remember to opt in —
        forgetting would silently write real bookkeeping files into
        whoever's actual $HOME runs this suite."""
        monkeypatch.setattr(ms, "_SCOPE_STATE_DIR", tmp_path / "_scope_state")

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


# ---------------------------------------------------------------------------
# Concurrency safety — fix for a blocking finding from an adversarial review
# of this module (see memnos_scope.py's module docstring "Concurrency
# safety" section for the full design). Tommy's own default operating mode
# dispatches a wave of several concurrent subagents into ONE workspace
# (core.md's wave-based fan-out), so two overlapping generate_scoping_files()
# / cleanup() lifetimes for the same workspace are the common case, not an
# edge case.
# ---------------------------------------------------------------------------


class TestConcurrentScoping:
    @pytest.fixture(autouse=True)
    def _isolate_scope_state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ms, "_SCOPE_STATE_DIR", tmp_path / "_scope_state")

    def test_two_overlapping_dispatches_restore_exact_original(self, tmp_path):
        """The exact bug reproduced by the adversarial review: two dispatches
        into the SAME workspace with overlapping lifetimes — generate A,
        generate B while A is still active, cleanup A, THEN cleanup B — must
        leave the workspace's pre-existing .mcp.json restored to its EXACT
        original bytes once both are done, not with the memnos block
        permanently spliced into it.

        On the pre-fix code this ordering is RED: A's cleanup restores A's
        own snapshot (the true original O, taken at A's generate() call).
        B's generate() call happened AFTER A's, so B's own independent
        snapshot is O+memnos (whatever was on disk at B's own generate()
        time — already merged by A). B's cleanup runs last and "wins,"
        writing its stale O+memnos snapshot back — the memnos block is left
        permanently merged into the real file. This is byte-for-byte the
        corruption the review reported: `{"mcpServers": {"github": {...},
        "memnos": {...}}}` surviving both dispatches completing.

        On the fix, both dispatches share ONE snapshot (taken once, by A,
        the true first holder) via a reference count — cleanup only
        actually restores once the LAST live holder releases, regardless of
        which one that happens to be.
        """
        original = {"mcpServers": {"github": {"command": "gh-mcp", "args": []}}}
        original_text = json.dumps(original, indent=2) + "\n"
        (tmp_path / ".mcp.json").write_text(original_text)

        scoping_a = ms.generate_scoping_files(tmp_path)
        scoping_b = ms.generate_scoping_files(tmp_path)  # overlapping: A still active

        merged = json.loads((tmp_path / ".mcp.json").read_text())
        assert "github" in merged["mcpServers"], "pre-existing server must survive the merge"
        assert "memnos" in merged["mcpServers"]

        # A (first to START) cleans up FIRST; B (second to start) cleans up
        # LAST — the exact interleaving the review's repro used.
        scoping_a.cleanup()
        still_scoped = json.loads((tmp_path / ".mcp.json").read_text())
        assert "memnos" in still_scoped["mcpServers"], (
            "workspace must remain scoped while dispatch B is still active — "
            "restoring on A's cleanup alone would pull the scoped config out "
            "from under B mid-flight"
        )
        assert "github" in still_scoped["mcpServers"]

        scoping_b.cleanup()
        assert (tmp_path / ".mcp.json").read_text() == original_text, (
            "cleanup must restore byte-identical original content once the LAST "
            "concurrent dispatch releases it, not permanently merge the memnos block"
        )

    def test_three_way_overlap_any_release_order_restores_original(self, tmp_path):
        """Same property, generalized to three concurrent holders released in
        a non-FIFO order (the two started later release first; the one
        started first releases last) — the shared snapshot/refcount must not
        depend on any particular release order, only on the count reaching
        zero."""
        original = {"mcpServers": {"other": {"command": "x", "args": []}}}
        original_text = json.dumps(original, indent=2) + "\n"
        (tmp_path / ".mcp.json").write_text(original_text)

        a = ms.generate_scoping_files(tmp_path)
        b = ms.generate_scoping_files(tmp_path)
        c = ms.generate_scoping_files(tmp_path)

        b.cleanup()
        assert "memnos" in json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]
        c.cleanup()
        assert "memnos" in json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]
        a.cleanup()  # first-started, last to release
        assert (tmp_path / ".mcp.json").read_text() == original_text

    def test_concurrent_threads_stress_no_corruption(self, tmp_path):
        """Real OS-thread concurrency, not just a deterministic call
        ordering: several threads race through generate_scoping_files() with
        a Barrier forcing genuinely simultaneous entry into the critical
        section, repeated across multiple rounds reusing the same workspace.
        Proves the fcntl.flock'd critical section actually serializes
        concurrent access rather than merely being present in the source —
        a lock that's never really contended would pass the deterministic
        test above vacuously (it doesn't require true concurrency, by
        design, to stay a reliable non-flaky test)."""
        original = {"mcpServers": {"other": {"command": "x", "args": []}}}
        original_text = json.dumps(original, indent=2) + "\n"
        (tmp_path / ".mcp.json").write_text(original_text)

        n_threads = 8
        n_rounds = 5

        for round_num in range(n_rounds):
            barrier = threading.Barrier(n_threads)
            results: list = [None] * n_threads
            errors: list[BaseException] = []

            def worker(i):
                try:
                    barrier.wait(timeout=5)  # force genuinely overlapping starts
                    scoping = ms.generate_scoping_files(tmp_path)
                    time.sleep(0.01)  # widen the overlap window further
                    results[i] = scoping
                except BaseException as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

            assert not errors, f"round {round_num}: worker thread(s) raised: {errors}"
            assert all(results), f"round {round_num}: not every thread got a ScopingFiles handle"

            mid = json.loads((tmp_path / ".mcp.json").read_text())
            assert "memnos" in mid["mcpServers"], f"round {round_num}: file not valid/scoped mid-round"
            assert "other" in mid["mcpServers"]

            release_errors: list[BaseException] = []

            def releaser(scoping):
                try:
                    scoping.cleanup()
                except BaseException as exc:
                    release_errors.append(exc)

            release_threads = [threading.Thread(target=releaser, args=(r,)) for r in results]
            for t in release_threads:
                t.start()
            for t in release_threads:
                t.join(timeout=10)

            assert not release_errors, f"round {round_num}: cleanup() raised: {release_errors}"
            assert (tmp_path / ".mcp.json").read_text() == original_text, (
                f"round {round_num}: workspace not restored to exact original bytes "
                f"after all {n_threads} concurrent holders released"
            )

    def test_crash_then_new_dispatch_self_heals_and_restores_exactly(self, tmp_path):
        """Simulates the abnormal-exit case WITHOUT a real process crash
        (see test_launch_harness_sigterm_scope.py for a real-subprocess/
        real-SIGTERM proof): a holder whose owning PID is no longer alive is
        left in the state with its scope never released. The next
        generate_scoping_files() call into the same workspace must detect
        that, restore the true original (verifying its hash first), and
        THEN start a fresh scope session — so its own eventual cleanup()
        restores exactly, not on top of a stale merge."""
        original = {"mcpServers": {"other": {"command": "x", "args": []}}}
        original_text = json.dumps(original, indent=2) + "\n"
        (tmp_path / ".mcp.json").write_text(original_text)

        scoping = ms.generate_scoping_files(tmp_path)
        merged = json.loads((tmp_path / ".mcp.json").read_text())
        assert "memnos" in merged["mcpServers"]

        # Simulate the owning process crashing: reach into the persisted
        # state and rewrite its holder's pid to one that cannot possibly be
        # alive, without calling cleanup(). This is the on-disk shape a real
        # SIGKILL would leave behind (see the SIGTERM test for a genuine
        # subprocess proof of the same recovery path).
        lock_path, state_path = ms._workspace_state_paths(tmp_path)
        state = json.loads(state_path.read_text())
        assert len(state["holders"]) == 1
        dead_pid = 2**30  # astronomically unlikely to be a real live pid
        for holder in state["holders"].values():
            holder["pid"] = dead_pid
        state_path.write_text(json.dumps(state))

        # A brand-new dispatch into the same (still-scoped, "crashed")
        # workspace must self-heal: restore the true original, THEN scope
        # fresh for itself.
        healed = ms.generate_scoping_files(tmp_path)
        healed_text = json.loads((tmp_path / ".mcp.json").read_text())
        assert "other" in healed_text["mcpServers"], "self-heal lost the pre-existing server entry"
        assert "memnos" in healed_text["mcpServers"]

        healed.cleanup()
        assert (tmp_path / ".mcp.json").read_text() == original_text, (
            "self-healed scope session must still restore to the true original on its own cleanup"
        )

    def test_external_edit_during_abandoned_scope_is_not_clobbered(self, tmp_path):
        """The verify-before-restore safety net: if a workspace is left in
        the abandoned-scope state (crashed holder) AND a human/other tool
        edits the file in the meantime (not just leaves it as our merge
        left it), self-healing must NOT blindly overwrite that edit with
        the old snapshot — that would silently destroy real, intentional
        content. It should recognize its recorded snapshot no longer
        applies and leave the file alone instead."""
        original = {"mcpServers": {"other": {"command": "x", "args": []}}}
        original_text = json.dumps(original, indent=2) + "\n"
        (tmp_path / ".mcp.json").write_text(original_text)

        scoping = ms.generate_scoping_files(tmp_path)
        lock_path, state_path = ms._workspace_state_paths(tmp_path)
        state = json.loads(state_path.read_text())
        for holder in state["holders"].values():
            holder["pid"] = 2**30
        state_path.write_text(json.dumps(state))

        # Someone/something edits the file AFTER the crash, unaware of the
        # abandoned scope — a legitimate, independent change.
        hand_edited = {"mcpServers": {"other": {"command": "x", "args": []}, "brand_new": {"command": "y"}}}
        hand_edited_text = json.dumps(hand_edited, indent=2) + "\n"
        (tmp_path / ".mcp.json").write_text(hand_edited_text)

        # A new dispatch arrives — must NOT clobber the hand edit with the
        # stale pre-crash snapshot.
        healed = ms.generate_scoping_files(tmp_path)
        after_heal = (tmp_path / ".mcp.json").read_text()
        assert "brand_new" in after_heal, (
            "self-healing overwrote a legitimate concurrent edit with a stale snapshot"
        )
        healed.cleanup()
