"""Claude Code plugin + token hygiene (#206, #207): session-token fallback, scoped-token
reuse, legacy unwiring on plugin install, and the plugin's own files.

Pure unit tests (mocked DB, temp HOME). The live proof that plugin hooks fire once (and
twice alongside legacy settings hooks) needs a real `claude` run and is documented in the PR.

Run: python tests/test_claude_plugin.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if (detail and not cond) else ""))
    PASS += bool(cond)
    FAIL += (not cond)


def test_session_token_precedence():
    print("=== _session_token: env > scoped claude-code token > admin > '' ===")
    import memnos_cli as cli
    cfg = {"admin_token": "ADMIN", "agent_tokens": {"claude-code": "SCOPED"}}
    with mock.patch.dict(os.environ, {"MEMNOS_TOKEN": "ENV"}):
        check("env wins", cli._session_token(cfg) == "ENV")
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("MEMNOS_TOKEN", None)
        check("scoped token beats admin", cli._session_token(cfg) == "SCOPED")
        check("admin when no scoped token", cli._session_token({"admin_token": "ADMIN"}) == "ADMIN")
        check("empty when nothing configured", cli._session_token({}) == "")


def test_scoped_token_reuse():
    print("=== _ensure_claude_token: reuse the persisted token, mint only when missing/invalid ===")
    import memnos_cli as cli
    from core.control import Control
    saved = []
    with mock.patch.dict(os.environ, {}, clear=False), \
         mock.patch.object(cli, "_conn", return_value=object()), \
         mock.patch.object(cli, "_principal_id", return_value=7), \
         mock.patch.object(cli, "save_config", side_effect=lambda c: saved.append(json.dumps(c))), \
         mock.patch.object(Control, "init"), mock.patch.object(Control, "grant"), \
         mock.patch.object(Control, "mint_token", return_value="mnk_NEW") as mint, \
         mock.patch.object(Control, "authenticate") as auth:
        os.environ.pop("MEMNOS_TOKEN", None)
        cfg = {}
        tok, _ = cli._ensure_claude_token(cfg)
        check("first run mints", tok == "mnk_NEW" and mint.call_count == 1)
        check("minted token is persisted", cfg["agent_tokens"]["claude-code"] == "mnk_NEW" and len(saved) == 1)
        auth.return_value = 7
        tok, _ = cli._ensure_claude_token(cfg)
        check("second run reuses (no new mint)", tok == "mnk_NEW" and mint.call_count == 1)
        auth.return_value = None
        mint.return_value = "mnk_NEWER"
        tok, _ = cli._ensure_claude_token(cfg)
        check("revoked/invalid saved token is replaced", tok == "mnk_NEWER" and mint.call_count == 2)
        auth.return_value = 99   # authenticates, but as a different principal
        mint.return_value = "mnk_THIRD"
        tok, _ = cli._ensure_claude_token(cfg)
        check("token belonging to another principal is not reused", tok == "mnk_THIRD")


def _legacy_home(tmp):
    claude = os.path.join(tmp, ".claude")
    os.makedirs(os.path.join(claude, "commands"))
    json.dump({"mcpServers": {"memnos": {"command": "/x/memnos", "args": ["mcp"]},
                              "other": {"command": "keep-me"}}, "unrelated": 1},
              open(os.path.join(tmp, ".claude.json"), "w"))
    mk = lambda c: {"hooks": [{"type": "command", "command": c}]}
    json.dump({"hooks": {
        "UserPromptSubmit": [mk("MEMNOS_TOKEN=mnk_x memnos hook recall"), mk("other-tool prompt")],
        "Stop": [mk("MEMNOS_TOKEN=mnk_x memnos hook remember")],
        "SessionStart": [mk("MEMNOS_TOKEN=mnk_x memnos hook status")],
        "PreToolUse": [mk("memnos hook enforce")]}, "model": "keep"},
        open(os.path.join(claude, "settings.json"), "w"))
    open(os.path.join(claude, "commands", "memnos.md"), "w").write("---\ndescription: memnos memory — x\n---\n")
    open(os.path.join(claude, "commands", "mine.md"), "w").write("keep")
    return claude


def test_unwire_legacy():
    print("=== _unwire_claude_legacy: removes only memnos-owned entries, keeps enforce hook ===")
    import memnos_cli as cli
    with tempfile.TemporaryDirectory() as tmp:
        claude = _legacy_home(tmp)
        removed = cli._unwire_claude_legacy(tmp, claude)
        d = json.load(open(os.path.join(tmp, ".claude.json")))
        s = json.load(open(os.path.join(claude, "settings.json")))
        check("reports all three removals", len(removed) == 3, removed)
        check("memnos MCP entry gone, other MCP server kept",
              "memnos" not in d["mcpServers"] and d["mcpServers"]["other"] and d["unrelated"] == 1)
        check("recall/remember/status hooks gone",
              "Stop" not in s["hooks"] and "SessionStart" not in s["hooks"])
        check("unrelated hook in same event kept",
              len(s["hooks"]["UserPromptSubmit"]) == 1 and "other-tool" in json.dumps(s["hooks"]["UserPromptSubmit"]))
        check("PreToolUse enforce hook and other settings untouched",
              "memnos hook enforce" in json.dumps(s["hooks"]["PreToolUse"]) and s["model"] == "keep")
        check("memnos command removed, user's own command kept",
              not os.path.exists(os.path.join(claude, "commands", "memnos.md"))
              and os.path.exists(os.path.join(claude, "commands", "mine.md")))
        check("backups were written", os.path.exists(os.path.join(tmp, ".claude.json.memnos-bak"))
              and os.path.exists(os.path.join(claude, "settings.json.memnos-bak")))
        check("idempotent: second run removes nothing", cli._unwire_claude_legacy(tmp, claude) == [])


def test_plugin_detection():
    print("=== _memnos_plugin_installed ===")
    import memnos_cli as cli
    with tempfile.TemporaryDirectory() as tmp:
        check("no installed_plugins.json -> False", cli._memnos_plugin_installed(tmp) is False)
        os.makedirs(os.path.join(tmp, "plugins"))
        p = os.path.join(tmp, "plugins", "installed_plugins.json")
        json.dump({"plugins": {"superpowers@x": []}}, open(p, "w"))
        check("other plugins only -> False", cli._memnos_plugin_installed(tmp) is False)
        json.dump({"plugins": {"memnos@memnos": [{}]}}, open(p, "w"))
        check("memnos@memnos installed -> True", cli._memnos_plugin_installed(tmp) is True)
        open(p, "w").write("{broken")
        check("corrupt file -> False, no crash", cli._memnos_plugin_installed(tmp) is False)


def test_plugin_files():
    print("=== plugin files ===")
    import memnos_cli as cli
    pdir = os.path.join(ROOT, "plugins", "memnos")
    mkt = json.load(open(os.path.join(ROOT, ".claude-plugin", "marketplace.json")))
    src = mkt["plugins"][0]["source"]
    check("marketplace source resolves to the plugin dir", os.path.isdir(os.path.join(ROOT, src)))
    check("plugin name matches marketplace entry",
          json.load(open(os.path.join(pdir, ".claude-plugin", "plugin.json")))["name"] == mkt["plugins"][0]["name"])
    hooks = json.load(open(os.path.join(pdir, "hooks", "hooks.json")))
    cmds = [h["command"] for g in hooks["hooks"].values() for e in g for h in e["hooks"]]
    check("hooks wrapped under top-level 'hooks' with recall+remember+status",
          {"UserPromptSubmit", "Stop", "SessionStart"} == set(hooks["hooks"]))
    blob = "".join(open(os.path.join(dp, f), encoding="utf-8").read()
                   for dp, _, fs in os.walk(pdir) for f in fs if not f.endswith(".png"))
    check("no token or namespace baked into any plugin file", "mnk_" not in blob and "MEMNOS_TOKEN=" not in blob
          and "MEMNOS_NS=" not in blob)
    import struct
    ic = open(os.path.join(pdir, ".claude-plugin", "icon.png"), "rb").read(24)
    w, h = struct.unpack(">II", ic[16:24])
    check("icon is a square PNG, 512-2048 px, under 2 MB (directory requirement)",
          ic[:8] == b"\x89PNG\r\n\x1a\n" and w == h and 512 <= w <= 2048
          and os.path.getsize(os.path.join(pdir, ".claude-plugin", "icon.png")) < 2_000_000)
    check("session-start script is executable", os.access(os.path.join(pdir, "scripts", "session-start.sh"), os.X_OK))
    expected = cli._SLASH_CMD.replace(" --token __MEMNOS_TOKEN__", "").replace("__MEMNOS_URL__", "http://127.0.0.1:8900")
    check("commands/memnos.md is generated from the claude-setup template (no drift)",
          open(os.path.join(pdir, "commands", "memnos.md"), encoding="utf-8").read() == expected)
    check("every hook command is token-free", all("mnk_" not in c for c in cmds))
    import re
    pv = json.load(open(os.path.join(pdir, ".claude-plugin", "plugin.json"))).get("version")
    for toml in ("pyproject.toml", "sdk/pyproject.toml"):
        v = re.search(r'^version = "([^"]+)"', open(os.path.join(ROOT, toml)).read(), re.M).group(1)
        check(f"plugin version {pv} matches {toml} ({v}) so plugin updates follow releases", pv == v)
    readme = open(os.path.join(pdir, "README.md"), encoding="utf-8").read()
    check("plugin README: >= 40 words and a Data handling section", len(readme.split()) >= 40 and "## Data handling" in readme)
    if shutil.which("claude"):
        r = subprocess.run(["claude", "plugin", "validate", ROOT], capture_output=True, text=True, timeout=60)
        check("`claude plugin validate` passes on the marketplace", r.returncode == 0, r.stdout[-300:] + r.stderr[-300:])
    else:
        print("  SKIP  claude CLI not installed — `claude plugin validate` not run")


def test_status_warns_when_running_as_admin():
    print("=== hook status: warns only when falling back to the admin token ===")
    def status(cfg):
        with tempfile.TemporaryDirectory() as h:
            os.makedirs(os.path.join(h, ".memnos"))
            json.dump({"port": 59999, **cfg}, open(os.path.join(h, ".memnos", "config.json"), "w"))
            r = subprocess.run([sys.executable, os.path.join(ROOT, "memnos_cli.py"), "hook", "status"],
                               input='{"session_id":"s","cwd":"/tmp"}', capture_output=True, text=True,
                               env={"HOME": h, "PATH": os.environ["PATH"]}, timeout=30)
            return json.loads(r.stdout).get("systemMessage", "")
    check("admin-only config -> warning", "using the admin token" in status({"admin_token": "mnk_a"}))
    check("scoped token present -> no warning",
          "admin token" not in status({"admin_token": "mnk_a", "agent_tokens": {"claude-code": "mnk_s"}}))


def main():
    test_session_token_precedence()
    test_scoped_token_reuse()
    test_unwire_legacy()
    test_plugin_detection()
    test_plugin_files()
    test_status_warns_when_running_as_admin()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
