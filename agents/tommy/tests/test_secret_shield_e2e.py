"""
End-to-end test for Secret Shield (issue #115): a `secret://NAME` reference
resolves to the correct value in the launched subprocess's ACTUAL
environment, over a real network round-trip to a real memnos server backed
by a real Postgres — no mocking of resolve_secret() or the HTTP layer.

Requires a live memnos server + Postgres — see conftest.py's module
docstring for how to stand one up locally (an isolated throwaway
pgvector/pgvector:pg16 container, MEMNOS_SECRET_KEY set explicitly,
OPENAI_API_KEY explicitly unset, server started with an isolated HOME).
Skips (or fails under TOMMY_REQUIRE_SECRET_SHIELD=1 — see conftest.py) if
none is reachable.

Covers, for BOTH launch call sites:
  - cli.py._launch_harness: full detail — the resolved value lands in the
    real subprocess environment under the configured var name, is absent
    from the actual bytes written to the prompt tempfile, and is absent
    from Tommy's own stdout/stderr (the only place it could leak besides
    the env itself and the prompt).
  - mcp_server.py.tommy_dispatch: the same env-correctness check, proving
    the wiring is symmetric across both entry points.
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest

import tommy.cli as cli_mod
import tommy.mcp_server as mcp_server_mod
from tommy.config import TommyConfig
from tommy.discovery.harnesses import HarnessSpec

FIXTURES = Path(__file__).parent / "fixtures"
ENV_CAPTURE_HARNESS = FIXTURES / "env_capture_harness.py"


def _capture_harness_spec(name: str = "capture-harness") -> HarnessSpec:
    return HarnessSpec(
        name=name,
        binary=sys.executable,
        launch_template=[
            sys.executable, str(ENV_CAPTURE_HARNESS),
            "--append-system-prompt-file", "{prompt_file}",
        ],
        supports_tools=True,
        supports_mcp=False,
        description="test env-capturing harness",
        available=True,
    )


@pytest.fixture
def secret_fixture(live_memnos):
    """Seeds one real secret in the server's Vault and mints an agent token
    authorized to resolve it (and to write to a scratch namespace, in case a
    future test in this module wants it)."""
    run = uuid.uuid4().hex[:10]
    secret_name = f"test_ss_e2e_{run}"
    secret_value = f"e2e-secret-plaintext-{run}"
    ns = f"test:secret-shield-e2e:{run}"

    call, admin_tok = live_memnos["call"], live_memnos["admin_tok"]
    st, _ = call("POST", "/admin/api/secrets", admin_tok,
                {"name": secret_name, "value": secret_value, "description": "issue #115 e2e test"})
    assert st == 200, f"failed to seed test secret via /admin/api/secrets: {st}"

    from core.control import Control, SECRET_NS_PREFIX
    conn = live_memnos["conn"]
    agent_id = live_memnos["make_principal"]("agent")
    Control.grant(conn, agent_id, f"{SECRET_NS_PREFIX}{secret_name}")
    Control.grant(conn, agent_id, ns, can_read=True, can_write=True)
    agent_tok = Control.mint_token(conn, agent_id, "test-secret-shield-e2e")

    yield {"name": secret_name, "value": secret_value, "namespace": ns, "token": agent_tok}

    from core.vault import Vault
    Vault.delete(conn, secret_name)
    live_memnos["cleanup_namespace"](ns)


class TestCliLaunchHarnessE2E:
    def test_secret_resolves_into_real_subprocess_env_and_never_into_prompt_or_stdout(
        self, secret_fixture, live_memnos, tmp_path, monkeypatch, capsys,
    ):
        env_var_name = "TOMMY_TEST_SECRET_VALUE"
        capture_file = tmp_path / "captured.json"
        monkeypatch.setenv("TOMMY_TEST_ENV_CAPTURE_FILE", str(capture_file))

        monkeypatch.setattr(cli_mod, "all_harnesses", lambda: {"capture-harness": _capture_harness_spec()})

        (tmp_path / ".git").mkdir()
        monkeypatch.chdir(tmp_path)

        cfg = TommyConfig(
            harness="capture-harness",
            skip_permissions=False,
            memnos_url=live_memnos["url"],
            memnos_token=secret_fixture["token"],
            tommy_ns=secret_fixture["namespace"],
            secret_env={env_var_name: f"secret://{secret_fixture['name']}"},
        )

        with pytest.raises(SystemExit) as exc_info:
            cli_mod._launch_harness(cfg, project_key=None, extra_args=(), memnos_client=None)
        assert exc_info.value.code == 0, "the stub harness exits 0 — launch must succeed"

        assert capture_file.exists(), "env_capture_harness.py never ran or never received TOMMY_TEST_ENV_CAPTURE_FILE"
        captured = json.loads(capture_file.read_text())

        # 1. The resolved value reached the REAL subprocess environment, under
        #    the configured var name, and it's the correct plaintext.
        assert captured["env"].get(env_var_name) == secret_fixture["value"], (
            f"expected {env_var_name}={secret_fixture['value']!r} in the launched "
            f"subprocess's real environment, got {captured['env'].get(env_var_name)!r}"
        )
        # It must be the resolved plaintext, never the raw reference string.
        assert not captured["env"].get(env_var_name, "").startswith("secret://")

        # 2. The secret value must be ABSENT from the prompt file content
        #    actually handed to the subprocess (build_prompt's real output,
        #    not a mock).
        assert secret_fixture["value"] not in captured["prompt"], (
            "the resolved secret value leaked into the prompt Tommy built and "
            "wrote to the harness's --append-system-prompt-file"
        )

        # 3. The secret value must be ABSENT from anything Tommy itself
        #    printed (stdout/stderr) — the only other place it could leak
        #    besides the subprocess env and the prompt. The "resolved N
        #    secret(s)" log line must name the env var, never the value.
        out = capsys.readouterr()
        assert secret_fixture["value"] not in out.out
        assert secret_fixture["value"] not in out.err
        assert env_var_name in out.out, "expected the resolved-secret log line to name the env var"


class TestMcpDispatchE2E:
    def test_secret_resolves_into_real_subprocess_env(self, secret_fixture, live_memnos, tmp_path, monkeypatch):
        """Same env-correctness proof as the CLI path, over tommy_dispatch —
        confirms the wiring is symmetric across both call sites, not just
        the interactive one."""
        env_var_name = "TOMMY_TEST_SECRET_VALUE_MCP"
        capture_file = tmp_path / "captured_mcp.json"
        monkeypatch.setenv("TOMMY_TEST_ENV_CAPTURE_FILE", str(capture_file))

        cfg = TommyConfig(
            harness="capture-harness",
            skip_permissions=False,
            memnos_url=live_memnos["url"],
            memnos_token=secret_fixture["token"],
            tommy_ns=secret_fixture["namespace"],
            secret_env={env_var_name: f"secret://{secret_fixture['name']}"},
        )
        monkeypatch.setattr(mcp_server_mod, "_cfg", cfg, raising=False)
        monkeypatch.setattr(mcp_server_mod, "_active_project", None, raising=False)
        monkeypatch.setattr(mcp_server_mod, "all_harnesses", lambda: {"capture-harness": _capture_harness_spec()})

        result = mcp_server_mod.tommy_dispatch(
            task="issue #115 e2e — no-op task, only the env matters here",
            harness="capture-harness",
            workspace=str(tmp_path),
            async_run=False,       # block until the stub harness exits — deterministic
            inject_memory=False,   # no dependency on recall ranking for this test
        )
        assert result.get("status") == "done", f"stub harness did not exit cleanly: {result}"
        assert result.get("secrets_resolved") == [env_var_name]

        assert capture_file.exists(), "env_capture_harness.py never ran under tommy_dispatch"
        captured = json.loads(capture_file.read_text())
        assert captured["env"].get(env_var_name) == secret_fixture["value"]
        assert secret_fixture["value"] not in captured["prompt"]
