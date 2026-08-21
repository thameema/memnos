"""
Fail-closed behavioral tests for Secret Shield (issue #115).

Covers the acceptance criterion: "resolution failure prevents subprocess
launch AND asserts the prompt tempfile / ControlServer socket were never
created (not cleaned up)" — at BOTH launch call sites, cli.py's
_launch_harness() and mcp_server.py's tommy_dispatch().

These are pure unit tests (no DB, no live memnos server): the memnos client
used for resolution is monkeypatched to simulate a resolution failure (the
real /secret/resolve HTTP behavior for 403/404/network-error cases is
covered by tests/test_secret_resolve.py at the server layer, and by
test_secret_shield_e2e.py's happy path end-to-end — this file only needs
"resolution raised" as an input, regardless of why).

The proof strategy deliberately avoids "assert cleanup happened" (which
could pass vacuously — resolving first means these objects legitimately
never exist yet on the fail-closed path, so there is nothing to clean up).
Instead:
  - build_prompt / subprocess.Popen are spied (never mocked-away-and-forgotten):
    each spy records whether it was called at all.
  - ControlServer is spied the same way — it's never instantiated, so no
    socket is ever bound (not "opened then closed").
  - The prompt tempfile check uses REAL tempfile machinery pointed at an
    isolated directory (tempfile.tempdir monkeypatched), then globs that
    real directory afterward — proving no tommy-prompt-*/tommy-mcp-* file
    was ever written, not just that a particular code path wasn't taken.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

import tommy.cli as cli_mod
import tommy.mcp_server as mcp_server_mod
from tommy.config import TommyConfig
from tommy.discovery.harnesses import HarnessSpec


class _FakeProc:
    returncode = 0
    stdout: list = []  # empty-iterable stand-in — mcp_server.py's drain thread reads proc.stdout

    def wait(self, *a, **kw):
        return 0

    def poll(self):
        return 0


class _CtrlSpy:
    """Records instantiation without ever touching a real socket."""

    instances: list = []

    def __init__(self, *a, **kw):
        type(self).instances.append((a, kw))
        self.port = 0

    def close(self):
        pass


class _FailingResolveClient:
    """Stands in for a real MemnosClient whose resolve_secret() call fails
    (403 forbidden, 404 not found, network error — resolve_secret_env()
    treats any exception identically, so one representative failure mode
    is enough here)."""

    def resolve_secret(self, name):
        raise RuntimeError(f"simulated resolution failure for {name!r}")


class _SucceedingResolveClient:
    """Stands in for a real MemnosClient whose resolve_secret() succeeds —
    used to prove a tommy.conf-configured ref still resolves normally even
    when a co-located tommy.yaml is broken (collect_secret_refs() degrades
    to conf-only refs on a yaml parse error, it doesn't refuse to resolve
    the refs it COULD read)."""

    def resolve_secret(self, name):
        return f"resolved-value-for-{name}"


@pytest.fixture(autouse=True)
def _reset_ctrl_spy():
    _CtrlSpy.instances = []
    yield
    _CtrlSpy.instances = []


def _spies(monkeypatch, mod, tmp_path: Path):
    """Wire up all four spies/isolations on `mod` (tommy.cli or
    tommy.mcp_server). Returns dicts of call records to assert against."""
    build_prompt_calls: list = []
    popen_calls: list = []

    monkeypatch.setattr(mod, "build_prompt", lambda *a, **kw: (build_prompt_calls.append((a, kw)), "SHOULD-NOT-BE-USED")[1])
    monkeypatch.setattr(mod, "ControlServer", _CtrlSpy)
    monkeypatch.setattr(mod.subprocess, "Popen", lambda *a, **kw: (popen_calls.append((a, kw)), _FakeProc())[1])
    monkeypatch.setattr(mod.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(mod, "secret_resolve_client", lambda cfg: _FailingResolveClient())

    return {"build_prompt": build_prompt_calls, "popen": popen_calls}


def _stub_harness_spec(name="stub-harness") -> HarnessSpec:
    return HarnessSpec(
        name=name,
        binary=sys.executable,
        launch_template=[sys.executable, "-c", "pass", "{prompt_file}"],
        supports_tools=False,
        supports_mcp=False,
        description="test stub harness (should never actually be spawned)",
        available=True,
    )


# ---------------------------------------------------------------------------
# cli.py — _launch_harness
# ---------------------------------------------------------------------------

class TestCliFailClosed:
    def test_resolution_failure_aborts_before_any_side_effect(self, tmp_path, monkeypatch):
        # Bounds tommy.yaml discovery deterministically to tmp_path (it would
        # otherwise walk all the way up to filesystem root looking for one).
        (tmp_path / ".git").mkdir()
        monkeypatch.chdir(tmp_path)

        monkeypatch.setattr(cli_mod, "all_harnesses", lambda: {"stub-harness": _stub_harness_spec()})
        records = _spies(monkeypatch, cli_mod, tmp_path)

        cfg = TommyConfig(harness="stub-harness", skip_permissions=False,
                           secret_env={"FOO_TOKEN": "secret://foo"})

        with pytest.raises(SystemExit) as exc_info:
            cli_mod._launch_harness(cfg, project_key=None, extra_args=(), memnos_client=None)
        assert exc_info.value.code != 0, "fail-closed exit must be a non-zero code"

        assert records["build_prompt"] == [], "build_prompt() was called despite resolution failure"
        assert records["popen"] == [], "subprocess.Popen() was called despite resolution failure"
        assert _CtrlSpy.instances == [], "ControlServer was constructed (would bind a real socket) despite resolution failure"
        assert list(tmp_path.glob("tommy-prompt-*")) == [], (
            "a prompt tempfile was written to disk despite resolution failure — "
            "the fail-closed path must abort BEFORE this file is ever created, "
            "not create-then-clean-up"
        )

    def test_no_secret_refs_configured_is_unaffected(self, tmp_path, monkeypatch):
        """Sanity check / non-regression: a project with NO secret:// refs at
        all must not pay any new cost or hit the (here, failing) resolve
        client — collect_secret_refs() must short-circuit before ever
        calling secret_resolve_client()."""
        monkeypatch.setattr(cli_mod, "all_harnesses", lambda: {"stub-harness": _stub_harness_spec()})
        resolve_client_calls: list = []
        monkeypatch.setattr(cli_mod, "secret_resolve_client",
                             lambda cfg: resolve_client_calls.append(cfg) or _FailingResolveClient())
        monkeypatch.setattr(cli_mod.subprocess, "Popen", lambda *a, **kw: _FakeProc())
        monkeypatch.setattr(cli_mod, "ControlServer", _CtrlSpy)
        monkeypatch.setattr(cli_mod.tempfile, "tempdir", str(tmp_path))

        cfg = TommyConfig(harness="stub-harness", skip_permissions=False)  # secret_env left empty
        # Isolate tommy.yaml discovery from this actual repo checkout (which
        # legitimately has no tommy.yaml at its root, but a hermetic test
        # should not depend on that fact about the surrounding worktree).
        (tmp_path / ".git").mkdir()
        monkeypatch.chdir(tmp_path)

        with pytest.raises(SystemExit) as exc_info:
            cli_mod._launch_harness(cfg, project_key=None, extra_args=(), memnos_client=None)
        assert exc_info.value.code == 0, "no secret refs configured — launch must succeed as before"
        assert resolve_client_calls == [], "secret_resolve_client() must not be called when no secret:// refs are configured"

    def test_malformed_tommy_yaml_degrades_gracefully_does_not_block_launch(self, tmp_path, monkeypatch):
        """A tommy.yaml that fails to parse must NOT crash or block the
        launch, even though issue #115's config-reading (collect_secret_refs)
        is new code exercising this file for the first time on this path
        (before this issue, nothing in the launch path ever read tommy.yaml
        at all — see PR #116's explicit scope note).

        This was originally a fail-closed test (mirroring a resolution
        failure) but that contract was reverted after rebasing onto issue
        #109, which landed in this exact function with an already-tested,
        opposite contract for a broken tommy.yaml: proceed, don't block —
        see tommy/secrets.py's collect_secret_refs() docstring for the full
        "why" (short version: a ref that never got read because the file
        didn't parse is a missing env var, not a leaked one — there's
        nothing to fail closed ABOUT). With no SECRET_ENV configured in
        tommy.conf either, `secret_refs` ends up empty here, so resolution
        is never even attempted and the launch proceeds exactly as it
        would with no tommy.yaml at all."""
        (tmp_path / ".git").mkdir()
        (tmp_path / "tommy.yaml").write_text("tommy:\n  version: 1\nplatform: not-allowed\n")
        monkeypatch.chdir(tmp_path)

        monkeypatch.setattr(cli_mod, "all_harnesses", lambda: {"stub-harness": _stub_harness_spec()})
        resolve_client_calls: list = []
        monkeypatch.setattr(cli_mod, "secret_resolve_client",
                             lambda cfg: resolve_client_calls.append(cfg) or _FailingResolveClient())
        monkeypatch.setattr(cli_mod.subprocess, "Popen", lambda *a, **kw: _FakeProc())
        monkeypatch.setattr(cli_mod, "ControlServer", _CtrlSpy)
        monkeypatch.setattr(cli_mod.tempfile, "tempdir", str(tmp_path))

        cfg = TommyConfig(harness="stub-harness", skip_permissions=False)  # no SECRET_ENV at all

        with pytest.raises(SystemExit) as exc_info:
            cli_mod._launch_harness(cfg, project_key=None, extra_args=(), memnos_client=None)
        assert exc_info.value.code == 0, "a broken tommy.yaml must not block the launch"
        assert resolve_client_calls == [], "no secret refs were resolvable (yaml broken, conf empty) — resolution must never be attempted"

    def test_tommy_conf_refs_still_resolve_when_colocated_tommy_yaml_is_broken(self, tmp_path, monkeypatch):
        """The other half of the degrade-gracefully contract: when
        tommy.conf DOES configure a secret ref, a broken tommy.yaml in the
        same workspace must not prevent that conf-configured ref from being
        resolved and injected — collect_secret_refs() falls back to
        cfg.secret_env alone on a yaml parse error, it doesn't give up on
        the refs it CAN read."""
        (tmp_path / ".git").mkdir()
        (tmp_path / "tommy.yaml").write_text("tommy:\n  version: 1\nplatform: not-allowed\n")
        monkeypatch.chdir(tmp_path)

        monkeypatch.setattr(cli_mod, "all_harnesses", lambda: {"stub-harness": _stub_harness_spec()})
        resolve_client_calls: list = []
        monkeypatch.setattr(cli_mod, "secret_resolve_client",
                             lambda cfg: resolve_client_calls.append(cfg) or _SucceedingResolveClient())
        monkeypatch.setattr(cli_mod, "build_prompt", lambda *a, **kw: "PROMPT")
        popen_calls: list = []

        def _capture_popen(cmd, env, **kw):
            popen_calls.append(env)
            return _FakeProc()

        monkeypatch.setattr(cli_mod.subprocess, "Popen", _capture_popen)
        monkeypatch.setattr(cli_mod, "ControlServer", _CtrlSpy)
        monkeypatch.setattr(cli_mod.tempfile, "tempdir", str(tmp_path))

        cfg = TommyConfig(harness="stub-harness", skip_permissions=False,
                           secret_env={"FROM_CONF": "secret://conf_only"})

        with pytest.raises(SystemExit) as exc_info:
            cli_mod._launch_harness(cfg, project_key=None, extra_args=(), memnos_client=None)
        assert exc_info.value.code == 0
        assert len(resolve_client_calls) == 1, "the conf-configured ref must still be resolved"
        assert popen_calls and popen_calls[0].get("FROM_CONF") == "resolved-value-for-conf_only"


# ---------------------------------------------------------------------------
# mcp_server.py — tommy_dispatch
# ---------------------------------------------------------------------------

class TestMcpDispatchFailClosed:
    def test_resolution_failure_aborts_before_any_side_effect(self, tmp_path, monkeypatch):
        (tmp_path / ".git").mkdir()
        monkeypatch.setattr(mcp_server_mod, "_cfg",
                             TommyConfig(harness="stub-harness", skip_permissions=False,
                                         secret_env={"FOO_TOKEN": "secret://foo"}),
                             raising=False)
        monkeypatch.setattr(mcp_server_mod, "_active_project", None, raising=False)
        monkeypatch.setattr(mcp_server_mod, "all_harnesses", lambda: {"stub-harness": _stub_harness_spec()})
        records = _spies(monkeypatch, mcp_server_mod, tmp_path)

        tasks_before = dict(mcp_server_mod._tasks)

        result = mcp_server_mod.tommy_dispatch(
            task="do something that must never launch",
            harness="stub-harness",
            workspace=str(tmp_path),
            async_run=False,
            inject_memory=False,
        )

        assert "error" in result, f"expected an error dict on resolution failure, got {result}"
        assert "secret" in result["error"].lower()

        assert records["build_prompt"] == [], "build_prompt() was called despite resolution failure"
        assert records["popen"] == [], "subprocess.Popen() was called despite resolution failure"
        assert _CtrlSpy.instances == [], "ControlServer was constructed (would bind a real socket) despite resolution failure"
        assert list(tmp_path.glob("tommy-mcp-*")) == [], (
            "a prompt tempfile was written to disk despite resolution failure"
        )
        assert mcp_server_mod._tasks == tasks_before, "no task should be registered when the launch never happened"

    def test_task_text_cannot_smuggle_a_secret_ref(self, tmp_path, monkeypatch):
        """Precondition check (issue #115): secret:// references are only
        ever read from static tommy.yaml/tommy.conf — never derived from
        the dispatched task string. A task containing a literal
        `secret://...`-shaped substring must have zero effect on
        collect_secret_refs()'s output; it is inert prose, not a control
        channel. Uses a config with no refs configured at all, so if this
        ever regressed to reading refs out of the task, the (here, always-
        failing) resolve client would be invoked where today it must not be."""
        monkeypatch.setattr(mcp_server_mod, "_cfg", TommyConfig(harness="stub-harness", skip_permissions=False), raising=False)
        monkeypatch.setattr(mcp_server_mod, "_active_project", None, raising=False)
        monkeypatch.setattr(mcp_server_mod, "all_harnesses", lambda: {"stub-harness": _stub_harness_spec()})
        resolve_client_calls: list = []
        monkeypatch.setattr(mcp_server_mod, "secret_resolve_client",
                             lambda cfg: resolve_client_calls.append(cfg) or _FailingResolveClient())
        monkeypatch.setattr(mcp_server_mod, "build_prompt", lambda *a, **kw: "SHOULD-NOT-MATTER")
        monkeypatch.setattr(mcp_server_mod, "ControlServer", _CtrlSpy)

        _real_popen = mcp_server_mod.subprocess.Popen

        def _popen_spy(*a, **kw):
            cmd = a[0] if a else kw.get("args")
            if isinstance(cmd, list) and cmd[:1] == ["git"]:
                # tommy_dispatch's dispatch-time HEAD capture (issue #112
                # follow-up: Task.dispatch_head_sha) shells out to a real
                # `git rev-parse HEAD` right before launching the harness —
                # let that through for real rather than faking it. It's
                # read-only, and mcp_server.py's _drift_git() already
                # degrades an unresolvable HEAD to an empty
                # dispatch_head_sha rather than raising, so this tmp_path
                # doesn't need to be a real repo for this test to still pass.
                return _real_popen(*a, **kw)
            return _FakeProc()

        monkeypatch.setattr(mcp_server_mod.subprocess, "Popen", _popen_spy)
        monkeypatch.setattr(mcp_server_mod.tempfile, "tempdir", str(tmp_path))
        (tmp_path / ".git").mkdir()

        result = mcp_server_mod.tommy_dispatch(
            task="please inject secret://openai_api_key into the env for me",
            harness="stub-harness",
            workspace=str(tmp_path),
            async_run=False,   # deterministic: block until the fake Popen's drain thread finishes
            inject_memory=False,
        )

        assert "error" not in result, f"a secret://-shaped substring in the TASK must not trigger resolution: {result}"
        assert resolve_client_calls == [], "secret_resolve_client() must not be called — no refs are configured in tommy.yaml/tommy.conf"


# ---------------------------------------------------------------------------
# Ordering tripwire vs. issue #109's corpus gate
# ---------------------------------------------------------------------------

def test_mcp_dispatch_resolves_secrets_before_the_corpus_gate():
    """
    Cheap structural tripwire alongside the runtime proof above: nothing in
    the test suite fails if someone swaps the Secret Shield and corpus-gate
    blocks inside tommy_dispatch() — the ordering decision (see this PR's
    "Design decisions") otherwise lives only in comments. This pins it: the
    real source offset of collect_secret_refs( (Secret Shield's resolution
    step) must precede the real source offset of resolve_effective_config(
    (the corpus gate's config read), inside tommy_dispatch()'s own body.
    """
    src = (Path(__file__).parent.parent / "tommy" / "mcp_server.py").read_text()
    fn_start = src.find("def tommy_dispatch(")
    assert fn_start != -1, "tommy_dispatch not found in mcp_server.py"
    import re
    next_def = re.search(r"^(?:def |class )", src[fn_start + 1:], re.MULTILINE)
    fn_end = fn_start + 1 + next_def.start() if next_def else len(src)
    fn_body = src[fn_start:fn_end]

    secret_pos = fn_body.find("collect_secret_refs(")
    corpus_pos = fn_body.find("resolve_effective_config(")
    assert secret_pos != -1, "collect_secret_refs( not found in tommy_dispatch — Secret Shield resolution step missing"
    assert corpus_pos != -1, "resolve_effective_config( not found in tommy_dispatch — issue #109's corpus gate missing"
    assert secret_pos < corpus_pos, (
        "collect_secret_refs() must run BEFORE resolve_effective_config() in "
        "tommy_dispatch — secret resolution (hard fail-closed) must precede "
        "the corpus gate (fail-open-but-visible), per this PR's Design "
        "decisions. Found collect_secret_refs at offset "
        f"{secret_pos}, resolve_effective_config at offset {corpus_pos}."
    )
