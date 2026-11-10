"""Antigravity (agy) support, issue #232: agent-setup wiring + the camelCase hook contract.

Pure unit tests: temp HOME, mocked token minting / server / network. Run:
    .venv/bin/pytest tests/test_antigravity_setup_and_hooks.py
"""
import argparse
import io
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import memnos_cli as cli  # noqa: E402


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MEMNOS_TOKEN", raising=False)
    monkeypatch.setattr(cli, "_ensure_claude_token", lambda cfg, extra_ns=None: ("mnk_test", "user:me"))
    monkeypatch.setattr(cli, "_mcp_launcher", lambda: ("/usr/local/bin/memnos", ["mcp"]))
    return tmp_path


def setup(agent, transport="stdio", namespace=None):
    cli.cmd_agent_setup(argparse.Namespace(agent=agent, namespace=namespace, force=True,
                                           agent_dir=None, transport=transport), {"port": 8900})


def cfgdir(home):
    return home / ".gemini" / "config"


# ---------------------------------------------------------------- setup
@pytest.mark.parametrize("alias", ["antigravity", "agy", "gemini"])
def test_setup_stdio_writes_all_three_files(home, alias):
    setup(alias)
    m = json.loads((cfgdir(home) / "mcp_config.json").read_text())["mcpServers"]["memnos"]
    assert m["command"] == "/usr/local/bin/memnos" and m["args"] == ["mcp"]
    assert m["env"] == {"MEMNOS_URL": "http://127.0.0.1:8900", "MEMNOS_TOKEN": "mnk_test", "MEMNOS_NS": "user:me"}
    h = json.loads((cfgdir(home) / "hooks.json").read_text())["memnos"]
    assert set(h) == {"SessionStart", "PreInvocation", "Stop", "PreToolUse"}
    assert h["SessionStart"][0]["command"].endswith("/usr/local/bin/memnos hook status")
    assert "MEMNOS_TOKEN=mnk_test" in h["PreInvocation"][0]["command"]
    assert h["Stop"][0]["command"].endswith("hook remember")
    pre = h["PreToolUse"][0]
    assert pre["matcher"] == "*" and pre["hooks"][0]["command"].endswith("hook enforce")
    assert "## memnos — long-term memory" in (cfgdir(home) / "GEMINI.md").read_text()


def test_setup_http_transport(home):
    setup("agy", transport="http")
    m = json.loads((cfgdir(home) / "mcp_config.json").read_text())["mcpServers"]["memnos"]
    assert m["url"] == "http://127.0.0.1:8900/mcp" and "command" not in m


def test_setup_is_idempotent_and_preserves_others(home):
    d = cfgdir(home)
    d.mkdir(parents=True)
    (d / "mcp_config.json").write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
    (d / "hooks.json").write_text(json.dumps({"other": {"Stop": [{"type": "command", "command": "y"}]}}))
    (d / "GEMINI.md").write_text("# mine\n")
    setup("agy"); first = {n: (d / n).read_text() for n in ("mcp_config.json", "hooks.json", "GEMINI.md")}
    setup("agy"); second = {n: (d / n).read_text() for n in first}
    assert first == second
    assert second["GEMINI.md"].count("## memnos — long-term memory") == 1
    assert second["GEMINI.md"].startswith("# mine")
    assert "other" in json.loads(second["mcp_config.json"])["mcpServers"]
    hooks = json.loads(second["hooks.json"])
    assert "other" in hooks and len(hooks["memnos"]["Stop"]) == 1
    assert (d / "hooks.json.memnos-bak").exists() and (d / "GEMINI.md.memnos-bak").exists()


def test_detection(home, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda n: None)
    assert "antigravity" not in cli._detect_installed_agents()
    (home / ".gemini" / "antigravity-cli").mkdir(parents=True)
    assert "antigravity" in cli._detect_installed_agents()
    monkeypatch.setattr("shutil.which", lambda n: "/bin/agy" if n == "agy" else None)
    (home / ".gemini" / "antigravity-cli").rmdir()
    assert "antigravity" in cli._detect_installed_agents()


def test_http_transport_still_rejected_for_others(home):
    with pytest.raises(SystemExit):
        setup("cursor", transport="http")


# ---------------------------------------------------------------- hooks
@pytest.fixture
def hook(monkeypatch, tmp_path, capsys):
    calls = {"remember": [], "recall": []}
    monkeypatch.setattr(cli, "_session_token", lambda cfg: "tok")
    monkeypatch.setattr(cli, "CONFIG_DIR", str(tmp_path / "memnos"))
    monkeypatch.setattr(cli, "_ENFORCE_CACHE_DIR", str(tmp_path / "enforce"))
    monkeypatch.setattr(cli, "_record_capture", lambda *a, **k: None)
    import nsresolve
    seen = {}

    def fake_resolve(data=None):
        seen["cwd"] = (data or {}).get("cwd")
        return "proj:x", "binding_repo"
    monkeypatch.setattr(nsresolve, "resolve_with_source", fake_resolve)
    monkeypatch.setattr(nsresolve, "resolve", lambda *a, **k: "proj:x")
    marks = set()

    def fake_first(sid, key, kind="dest"):
        if kind == "dest":
            return False
        new = (sid, key, kind) not in marks
        marks.add((sid, key, kind))
        return new
    monkeypatch.setattr(nsresolve, "session_first_time", fake_first)
    monkeypatch.setattr(nsresolve, "refresh", lambda **k: None)
    monkeypatch.setattr(cli, "_server_up", lambda *a, **k: True)
    monkeypatch.setattr(cli, "_refresh_enforce_cache", lambda cfg, ns: 0)
    monkeypatch.setattr(cli, "_fetch_nudges", lambda *a, **k: [])

    class Resp(io.BytesIO):
        pass

    def fake_urlopen(req, timeout=0):
        body = json.loads(req.data)
        if req.full_url.endswith("/recall"):
            calls["recall"].append(body)
            return Resp(json.dumps({"context": "- fact A", "memories": [1]}).encode())
        calls["remember"].append(body)
        return Resp(b"{}")
    monkeypatch.setattr(cli.urllib.request, "urlopen", fake_urlopen)

    def run(which, payload):
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        capsys.readouterr()
        cli.cmd_hook(argparse.Namespace(which=which), {"port": 8900})
        return capsys.readouterr().out
    run.calls, run.seen = calls, seen
    return run


def test_is_antigravity_hook():
    assert cli._is_antigravity_hook({"conversationId": "c"})
    assert cli._is_antigravity_hook({"toolCall": {}})
    assert not cli._is_antigravity_hook({"session_id": "s", "prompt": "p", "cwd": "/"})


def test_status_injects_step_and_uses_workspace_path(hook):
    out = json.loads(hook("status", {"conversationId": "c1", "workspacePaths": ["/w/proj", "/w/b"]}))
    msg = out["injectSteps"][0]["systemMessage"]["systemMessage"]
    assert msg.startswith("memnos:") and "memory ACTIVE" in msg
    assert out["injectSteps"][0]["systemMessage"]["metadata"] == {"user_markdown": msg}
    assert hook.seen["cwd"] == "/w/proj"


def test_recall_injects_and_dedupes_inner_invocations(hook):
    p = {"conversationId": "c1", "workspacePaths": ["/w"], "lastUserInput": "what did we decide about staging?",
         "invocationNum": 1}
    out = json.loads(hook("recall", p))
    text = out["injectSteps"][0]["systemMessage"]["systemMessage"]
    assert text.startswith("## Relevant memories (memnos)\n") and "fact A" in text
    assert "metadata" not in out["injectSteps"][0]["systemMessage"]
    assert hook.calls["recall"][0]["query"] == p["lastUserInput"]
    assert hook.calls["recall"][0]["session_id"] == "c1"
    n = len(hook.calls["recall"])
    assert json.loads(hook("recall", {**p, "invocationNum": 3})) == {}
    assert len(hook.calls["recall"]) == n
    # a NEW prompt recalls even when the counter did not reset
    out = json.loads(hook("recall", {**p, "invocationNum": 5, "lastUserInput": "a brand new question here?"}))
    assert "fact A" in out["injectSteps"][0]["systemMessage"]["systemMessage"]
    assert len(hook.calls["recall"]) == n + 1
    # the same prompt resubmitted in a later turn (invocationNum 1) recalls again
    hook("recall", {**p, "invocationNum": 1})
    assert len(hook.calls["recall"]) == n + 2


def test_recall_empty_prompt_prints_empty_object(hook):
    assert json.loads(hook("recall", {"conversationId": "c", "lastUserInput": ""})) == {}


def test_claude_recall_unchanged(hook):
    out = json.loads(hook("recall", {"session_id": "s", "prompt": "what did we decide about staging?"}))
    assert "injectSteps" not in out and "hookSpecificOutput" in out


def write_enforce_cache(cli_mod, rules):
    os.makedirs(cli_mod._ENFORCE_CACHE_DIR, exist_ok=True)
    json.dump({"rules": rules}, open(cli_mod._enforce_cache_path("proj:x"), "w"))


def rule(matcher, level="block"):
    return {"id": 7, "enforce_level": level, "tool_matcher": matcher, "rule_text": "no way"}


def test_enforce_allow_without_cache_or_match(hook):
    p = {"conversationId": "c", "toolCall": {"name": "run_command", "args": {"CommandLine": "ls"}}}
    assert json.loads(hook("enforce", p)) == {"decision": "allow"}
    write_enforce_cache(cli, [rule("run_command(rm -rf*)")])
    assert json.loads(hook("enforce", p)) == {"decision": "allow"}


@pytest.mark.parametrize("matcher", ["run_command(rm -rf*)", "Bash(rm -rf*)"])
def test_enforce_blocks_run_command_with_either_syntax(hook, matcher):
    write_enforce_cache(cli, [rule(matcher)])
    p = {"conversationId": "c", "toolCall": {"name": "run_command", "args": {"CommandLine": "rm -rf /tmp/x"}}}
    out = json.loads(hook("enforce", p))
    assert out["decision"] == "deny" and "no way" in out["reason"]


def test_enforce_ask_for_file_tools(hook):
    write_enforce_cache(cli, [rule("write_file(*.env)", "ask")])
    p = {"conversationId": "c", "toolCall": {"name": "write_file", "args": {"Path": "/a/.env"}}}
    assert json.loads(hook("enforce", p))["decision"] == "ask"


def test_enforce_claude_contract_unchanged(hook):
    write_enforce_cache(cli, [rule("Bash(rm -rf*)")])
    out = json.loads(hook("enforce", {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}}))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert hook("enforce", {"tool_name": "Bash", "tool_input": {"command": "ls"}}) == ""


def test_tool_match_subject_antigravity():
    f = cli._tool_match_subject
    assert f("run_command", {"CommandLine": "ls -la"}) == "run_command(ls -la)"
    for t in ("read_file", "write_file", "edit_file"):
        assert f(t, {"Path": "/p"}) == f"{t}(/p)"
    assert f("Bash", {"command": "ls"}) == "Bash(ls)" and f("Edit", {"file_path": "/f"}) == "Edit(/f)"


def transcript(tmp_path, steps):
    p = tmp_path / "transcript.jsonl"
    p.write_text("\n".join(json.dumps(s) for s in steps) + "\n")
    return str(p)


def test_remember_parses_transcript_and_strips_wrappers(hook, tmp_path):
    tp = transcript(tmp_path, [
        {"type": "USER_INPUT", "content": "<ADDITIONAL_METADATA>cwd=/x</ADDITIONAL_METADATA>\n"
                                          "<USER_REQUEST>first question about staging db</USER_REQUEST>"},
        {"type": "PLANNER_RESPONSE", "content": "old reply that should be dropped by the later turn"},
        {"type": "USER_INPUT", "content": "<USER_REQUEST>Please pick Postgres 16 for staging</USER_REQUEST>"},
        {"type": "RUN_COMMAND", "source": "MODEL", "content": "tool output must not count"},
        {"type": "PLANNER_RESPONSE", "content": "Decision: we use Postgres 16 for staging, ticket #232."},
    ])
    out = hook("remember", {"conversationId": "c", "workspacePaths": ["/w"], "transcriptPath": tp})
    assert json.loads(out) == {}
    saved = [(r["speaker"], r["text"]) for r in hook.calls["remember"]]
    assert saved == [("user", "Please pick Postgres 16 for staging"),
                     ("assistant", "Decision: we use Postgres 16 for staging, ticket #232.")]


def test_remember_falls_back_to_last_user_input_and_always_prints_json(hook, tmp_path):
    out = hook("remember", {"conversationId": "c", "lastUserInput":
                            "<USER_REQUEST>remember that staging uses port 5433</USER_REQUEST>",
                            "transcriptPath": str(tmp_path / "missing.jsonl")})
    assert json.loads(out) == {}
    assert [r["text"] for r in hook.calls["remember"]] == ["remember that staging uses port 5433"]


def test_remember_prints_json_even_on_internal_error(hook, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_hook_remember", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"conversationId": "c"})))
    capsys.readouterr()
    with pytest.raises(RuntimeError):
        cli.cmd_hook(argparse.Namespace(which="remember"), {"port": 8900})
    # the Stop contract (stdout "{}") is satisfied by the finally block before the error propagates
    assert capsys.readouterr().out.strip() == "{}"


def test_tommy_plugin_session_start_hook_always_emits_valid_json():
    import subprocess
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    hooks = json.load(open(os.path.join(root, "plugins", "tommy", "hooks.json")))
    cmd = hooks["tommy"]["SessionStart"][0]["command"]
    # When tommy is on PATH -> "{}"
    r_ok = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, check=True)
    assert json.loads(r_ok.stdout.strip()) == {} or "injectSteps" in json.loads(r_ok.stdout.strip())
    # When PATH is empty (tommy missing) -> valid JSON with injectSteps
    r_missing = subprocess.run(["/bin/sh", "-c", cmd], env={"PATH": "/usr/bin:/bin"},
                               capture_output=True, text=True, check=True)
    parsed = json.loads(r_missing.stdout.strip())
    assert "tommy CLI is not on PATH" in parsed["injectSteps"][0]["systemMessage"]["systemMessage"]

