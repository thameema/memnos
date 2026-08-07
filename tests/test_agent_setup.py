"""Integration-config CONTRACT tests for `memnos agent-setup` — how we test integrations
WITHOUT the actual tools installed. Each agent integration is just a config file in a
documented format; these tests run every writer against a temp HOME and assert the
contract the real tool expects:

  • correct file path (per agent, per platform)
  • memnos entry present under the right key (mcpServers / mcp.servers / mcp_servers / toml)
  • full env triple (MEMNOS_URL + MEMNOS_TOKEN + MEMNOS_NS) — a missing token = the
    "empty Bearer" 401s seen in the field
  • ABSOLUTE command path (GUI apps spawn MCP servers with a minimal PATH)
  • merge, not clobber: pre-existing servers/settings survive
  • idempotent: running twice leaves ONE memnos entry

What this can't prove is that the tool *reads* the file — that part is the tool's
documented contract (+ manual smoke tests). Run: python tests/test_agent_setup.py
"""
import json
import os
import subprocess
import sys
import tempfile

import psycopg

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable
PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += cond; FAIL += (not cond)


def run(home, *args, extra_env=None):
    """extra_env: dict merged into the subprocess env after the defaults below — a value
    of None DELETES that key, so a test can force e.g. MEMNOS_TOKEN absent regardless of
    what the host shell happens to export."""
    env = dict(os.environ, MEMNOS_DSN=DSN, HOME=home)
    try:
        for line in open(os.path.join(ROOT, ".env")):
            if line.strip().startswith("MEMNOS_SECRET_KEY=") and "MEMNOS_SECRET_KEY" not in os.environ:
                env["MEMNOS_SECRET_KEY"] = line.strip().split("=", 1)[1]
    except FileNotFoundError:
        pass
    if extra_env:
        for k, v in extra_env.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
    r = subprocess.run([PY, os.path.join(ROOT, "memnos_cli.py"), *args],
                       capture_output=True, text=True, env=env, timeout=60)
    return r.returncode, (r.stdout + r.stderr)


def entry_ok(e):
    """The contract every MCP client needs: absolute command + the full env triple."""
    env = e.get("env", {})
    return (os.path.isabs(e.get("command", "")) and e.get("args", [])[-1:] == ["mcp"]
            and env.get("MEMNOS_URL", "").startswith("http")
            and env.get("MEMNOS_TOKEN", "").startswith("mnk_")
            and bool(env.get("MEMNOS_NS")))


def json_agent(home, agent, relpath, keypath, seed):
    """Generic contract check for a JSON-config agent."""
    path = os.path.join(home, relpath)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(seed, f)
    rc, out = run(home, "agent-setup", agent)
    check(f"{agent}: exits 0", rc == 0)
    run(home, "agent-setup", agent)                       # idempotency: run twice
    d = json.load(open(path))
    node = d
    for k in keypath:
        node = node.get(k, {})
    check(f"{agent}: memnos entry valid (abs cmd + URL/TOKEN/NS)", entry_ok(node.get("memnos", {})))
    check(f"{agent}: merged, existing server preserved", "other" in node)
    check(f"{agent}: single memnos entry after re-run",
          sum(1 for k in node if k == "memnos") == 1)
    return d


def main():
    print("=== agent-setup integration contracts ===")
    home = tempfile.mkdtemp(prefix="memnos_agents_")

    # --- cursor / windsurf / claude-desktop: standard mcpServers JSON ---
    json_agent(home, "cursor", ".cursor/mcp.json",
               ("mcpServers",), {"mcpServers": {"other": {"command": "x"}}})
    json_agent(home, "windsurf", ".codeium/windsurf/mcp_config.json",
               ("mcpServers",), {"mcpServers": {"other": {"command": "x"}}})
    cd_rel = ("Library/Application Support/Claude/claude_desktop_config.json"
              if sys.platform == "darwin" else ".config/Claude/claude_desktop_config.json")
    d = json_agent(home, "claude-desktop", cd_rel,
                   ("mcpServers",), {"mcpServers": {"other": {"command": "x"}}, "theme": "dark"})
    check("claude-desktop: unrelated settings preserved", d.get("theme") == "dark")
    sk = os.path.join(home, ".memnos/claude-desktop-skill/SKILL.md")
    check("claude-desktop: memory skill written",
          os.path.exists(sk) and "memnos-memory" in open(sk).read()
          and "VERBATIM" in open(sk).read())

    # --- openclaw: nested mcp.servers ---
    json_agent(home, "openclaw", ".openclaw/openclaw.json",
               ("mcp", "servers"),
               {"gateway": {"port": 18789}, "mcp": {"servers": {"other": {"command": "x"}}}})

    # --- hermes: YAML mcp_servers ---
    import yaml
    hp = os.path.join(home, ".hermes/config.yaml")
    os.makedirs(os.path.dirname(hp), exist_ok=True)
    with open(hp, "w") as f:
        f.write("model: hermes-4\nmcp_servers:\n  existing:\n    command: foo\n")
    rc, out = run(home, "agent-setup", "hermes")
    check("hermes: exits 0", rc == 0)
    run(home, "agent-setup", "hermes")
    y = yaml.safe_load(open(hp))
    check("hermes: memnos entry valid", entry_ok(y.get("mcp_servers", {}).get("memnos", {})))
    check("hermes: existing server + model key preserved",
          "existing" in y.get("mcp_servers", {}) and y.get("model") == "hermes-4")
    # --- Bug 3: an AUTONOMOUS agent gets its OWN principal+token scoped to agent:<name>,
    # NOT the human user's token (which has no grant on agent:hermes → writes 403). ---
    hermes_env = y.get("mcp_servers", {}).get("memnos", {}).get("env", {})
    hermes_ns = hermes_env.get("MEMNOS_NS", "")
    hermes_tok = hermes_env.get("MEMNOS_TOKEN", "")
    check("hermes: namespace scoped to agent:hermes (not user:*)", hermes_ns == "agent:hermes")
    # Prove the wired token can actually WRITE to its own namespace (the field failure was 403).
    env = dict(os.environ, MEMNOS_DSN=DSN, HOME=home)
    r = subprocess.run([PY, os.path.join(ROOT, "memnos_admin.py"), "whoami", "agent:hermes", hermes_tok],
                       capture_output=True, text=True, env=env, timeout=30)
    who = r.stdout + r.stderr
    check("hermes: wired token authorizes WRITE on agent:hermes (Bug 3 fix)",
          "auth OK" in who and "write=True" in who)

    # --- codex: TOML appended + AGENTS.md ---
    cp = os.path.join(home, ".codex/config.toml")
    os.makedirs(os.path.dirname(cp), exist_ok=True)
    open(cp, "w").write('model = "o4"\n')
    rc, out = run(home, "agent-setup", "codex")
    check("codex: exits 0", rc == 0)
    run(home, "agent-setup", "codex")
    t = open(cp).read()
    check("codex: single mcp_servers.memnos block, existing kept",
          t.count("[mcp_servers.memnos]") == 1 and 'model = "o4"' in t)
    check("codex: token + ns in env block", "MEMNOS_TOKEN" in t and "MEMNOS_NS" in t)
    check("codex: AGENTS.md instruction written",
          "memnos" in open(os.path.join(home, ".codex/AGENTS.md")).read())

    # --- Bug: agent-setup --namespace <custom> must GRANT the wired principal on the
    # ACTUAL namespace passed, or every write 403s and is silently swallowed. ---
    cur_ns = os.path.join(home, ".cursor/mcp.json")
    with open(cur_ns, "w") as f:                          # reset to a clean seed
        json.dump({"mcpServers": {}}, f)
    rc, out = run(home, "agent-setup", "cursor", "--namespace", "foo:bar")
    check("cursor --namespace: exits 0", rc == 0)
    cn = json.load(open(cur_ns)).get("mcpServers", {}).get("memnos", {}).get("env", {})
    check("cursor --namespace: wired ns is the custom namespace", cn.get("MEMNOS_NS") == "foo:bar")
    env = dict(os.environ, MEMNOS_DSN=DSN, HOME=home)
    r = subprocess.run([PY, os.path.join(ROOT, "memnos_admin.py"),
                        "whoami", "foo:bar", cn.get("MEMNOS_TOKEN", "")],
                       capture_output=True, text=True, env=env, timeout=30)
    who = r.stdout + r.stderr
    check("cursor --namespace: wired token authorizes WRITE on foo:bar (no 403)",
          "auth OK" in who and "write=True" in who)

    # autonomous agent with a custom --namespace: same grant requirement.
    with open(hp, "w") as f:
        f.write("model: hermes-4\nmcp_servers:\n  existing:\n    command: foo\n")
    rc, out = run(home, "agent-setup", "hermes", "--namespace", "team:research")
    hn = yaml.safe_load(open(hp)).get("mcp_servers", {}).get("memnos", {}).get("env", {})
    check("hermes --namespace: wired ns is the custom namespace",
          hn.get("MEMNOS_NS") == "team:research")
    r = subprocess.run([PY, os.path.join(ROOT, "memnos_admin.py"),
                        "whoami", "team:research", hn.get("MEMNOS_TOKEN", "")],
                       capture_output=True, text=True, env=env, timeout=30)
    who = r.stdout + r.stderr
    check("hermes --namespace: wired token authorizes WRITE on team:research (no 403)",
          "auth OK" in who and "write=True" in who)

    # --- claude-code routes to the full Claude Code setup ---
    os.makedirs(os.path.join(home, ".claude"), exist_ok=True)
    rc, out = run(home, "agent-setup", "claude-code")
    check("claude-code: exits 0 + wires hooks", rc == 0 and "hooks" in out)
    s = json.load(open(os.path.join(home, ".claude/settings.json")))
    hook_cmds = json.dumps(s.get("hooks", {}))
    check("claude-code: recall+remember hooks with token",
          "memnos hook recall" in hook_cmds and "memnos hook remember" in hook_cmds
          and "MEMNOS_TOKEN=mnk_" in hook_cmds)
    cj = json.load(open(os.path.join(home, ".claude.json")))
    check("claude-code: MCP entry valid (abs cmd + env triple)",
          entry_ok(cj.get("mcpServers", {}).get("memnos", {})))
    # issue #27: constraint/remember/cheat-sheet verbs live in the SHIPPED template, so
    # they survive claude-setup / upgrade regeneration (not a one-off local edit).
    slash_cmd = open(os.path.join(home, ".claude/commands/memnos.md")).read()
    check("claude-code: /memnos template ships the constraint verb",
          "constraint <rule>" in slash_cmd and "--type constraint" in slash_cmd)
    check("claude-code: /memnos template ships the remember verb",
          "remember <fact>" in slash_cmd)
    check("claude-code: /memnos template ships the cheat sheet (?/help/cheat)",
          '"?"|help|cheat' in slash_cmd)
    # issue #27 field report: the slash command must never depend on config.json's
    # admin_token fallback — remember/recall carry their own real --token arg, and no
    # unrendered placeholder should survive into the shipped file.
    check("claude-code: /memnos template has NO unrendered auth placeholders",
          "__MEMNOS_URL__" not in slash_cmd and "__MEMNOS_TOKEN__" not in slash_cmd)
    check("claude-code: /memnos template's remember/recall calls carry a real --token arg",
          slash_cmd.count("--token mnk_") == 3)
    check("claude-code: no ENV=val prefix on any memnos call (breaks Bash(memnos:*) matching)",
          "MEMNOS_TOKEN=mnk_" not in slash_cmd and "MEMNOS_URL=http" not in slash_cmd)
    check("claude-code: /memnos template's admin console URL is rendered with the real URL",
          "/admin" in slash_cmd and "__MEMNOS_URL__/admin" not in slash_cmd)

    # issue #27 field report: a blank config.json admin_token 401s bare `memnos recall/
    # remember`. claude-setup must self-heal it (re-populate a fresh admin-service token).
    cfg_path = os.path.join(home, ".memnos/config.json")
    cfg_on_disk = json.load(open(cfg_path))
    cfg_on_disk["admin_token"] = ""
    json.dump(cfg_on_disk, open(cfg_path, "w"))
    rc, out = run(home, "agent-setup", "claude-code")
    check("claude-code: re-run with blank admin_token exits 0", rc == 0)
    healed = json.load(open(cfg_path))
    check("claude-code: self-heals a blank config.json admin_token",
          bool(healed.get("admin_token")) and healed["admin_token"].startswith("mnk_"))

    # issue #28: PreToolUse enforcement hook is auto-wired ONLY once an ask/block
    # constraint actually exists somewhere — opt-in through use, not a flag. Verify BOTH
    # directions: absent by default, present (with the right shape) once a constraint exists.
    s = json.load(open(os.path.join(home, ".claude/settings.json")))
    check("claude-code: NO PreToolUse hook wired before any enforced constraint exists",
          "PreToolUse" not in s.get("hooks", {})
          or "memnos hook enforce" not in json.dumps(s["hooks"]["PreToolUse"]))
    env = dict(os.environ, MEMNOS_DSN=DSN, HOME=home)
    _AGENT_SETUP_TEST_NS = "test:agent_setup_enforce_wiring"
    cadd = subprocess.run([PY, os.path.join(ROOT, "memnos_cli.py"), "constraint", "add",
                           _AGENT_SETUP_TEST_NS, "never rm -rf without confirmation",
                           "--enforce", "block", "--tool", "Bash(rm*)"],
                          capture_output=True, text=True, env=env, timeout=30)
    check("claude-code: constraint add (setup fixture) exits 0", cadd.returncode == 0)
    rc, out = run(home, "agent-setup", "claude-code")
    check("claude-code: re-run after a constraint exists exits 0", rc == 0)
    s = json.load(open(os.path.join(home, ".claude/settings.json")))
    ptu = s.get("hooks", {}).get("PreToolUse", [])
    memnos_groups = [g for g in ptu if "memnos hook enforce" in json.dumps(g)]
    check("claude-code: PreToolUse hook IS wired once a constraint exists",
          len(memnos_groups) == 1)
    check("claude-code: PreToolUse group carries matcher '*' and the enforce command",
          memnos_groups and memnos_groups[0].get("matcher") == "*"
          and "memnos hook enforce" in memnos_groups[0]["hooks"][0]["command"])
    check("claude-code: PreToolUse command carries a real inline token (not a placeholder)",
          memnos_groups and "MEMNOS_TOKEN=mnk_" in memnos_groups[0]["hooks"][0]["command"])
    # cleanup: don't leave this fixture constraint (pinned memory + control-plane row) behind
    subprocess.run([PY, os.path.join(ROOT, "memnos_cli.py"), "namespace", "rm",
                    _AGENT_SETUP_TEST_NS, "--purge"], capture_output=True, text=True, env=env, timeout=30)
    _dbconn = psycopg.connect(DSN, autocommit=True)
    with _dbconn.cursor() as _c:
        _c.execute("DELETE FROM memnos_control.constraint_enforcement WHERE namespace=%s",
                  (_AGENT_SETUP_TEST_NS,))
    _dbconn.close()

    # --- omnigent: inline `tools.memnos` MCP entry in an agent's config.yaml ---
    # Omnigent has no single global config shared by every agent (each agent is its own
    # config.yaml or directory bundle), so the writer is targeted via --agent-dir and
    # falls back to ~/.omnigent/config.yaml's `default_agent` when omitted.
    agent_dir = os.path.join(home, "my_agent")
    os.makedirs(agent_dir, exist_ok=True)
    agent_cfg = os.path.join(agent_dir, "config.yaml")
    with open(agent_cfg, "w") as f:
        yaml.safe_dump({"spec_version": 1, "name": "my_agent",
                         "tools": {"agents": ["researcher", "reviewer"]}}, f)
    rc, out = run(home, "agent-setup", "omnigent", "--agent-dir", agent_dir)
    check("omnigent: exits 0", rc == 0)
    run(home, "agent-setup", "omnigent", "--agent-dir", agent_dir)   # idempotency: run twice
    oc = yaml.safe_load(open(agent_cfg))
    otools = oc.get("tools", {})
    check("omnigent: memnos entry valid (abs cmd + URL/TOKEN/NS)", entry_ok(otools.get("memnos", {})))
    check("omnigent: memnos entry is inline type: mcp", otools.get("memnos", {}).get("type") == "mcp")
    check("omnigent: pre-existing tools.agents preserved", otools.get("agents") == ["researcher", "reviewer"])
    check("omnigent: single memnos entry after re-run",
          sum(1 for k in otools if k == "memnos") == 1)

    # --agent-dir pointing straight at a *.yaml file (not a directory) must also work.
    lone_cfg = os.path.join(home, "lone_agent.yaml")
    with open(lone_cfg, "w") as f:
        yaml.safe_dump({"spec_version": 1, "name": "lone_agent"}, f)
    rc, out = run(home, "agent-setup", "omnigent", "--agent-dir", lone_cfg)
    check("omnigent: exits 0 for a direct *.yaml file target", rc == 0)
    check("omnigent: wired the direct file (not a config.yaml alongside it)",
          entry_ok(yaml.safe_load(open(lone_cfg)).get("tools", {}).get("memnos", {})))

    # No --agent-dir: falls back to ~/.omnigent/config.yaml's default_agent.
    os.makedirs(os.path.join(home, ".omnigent"), exist_ok=True)
    default_agent_dir = os.path.join(home, "default_agent")
    os.makedirs(default_agent_dir, exist_ok=True)
    with open(os.path.join(default_agent_dir, "config.yaml"), "w") as f:
        yaml.safe_dump({"spec_version": 1, "name": "default_agent"}, f)
    with open(os.path.join(home, ".omnigent", "config.yaml"), "w") as f:
        yaml.safe_dump({"default_agent": default_agent_dir}, f)
    rc, out = run(home, "agent-setup", "omnigent")
    check("omnigent: exits 0 via default_agent fallback (no --agent-dir)", rc == 0)
    check("omnigent: default_agent's config.yaml got wired",
          entry_ok(yaml.safe_load(open(os.path.join(default_agent_dir, "config.yaml")))
                   .get("tools", {}).get("memnos", {})))

    # No --agent-dir AND no ~/.omnigent/config.yaml at all: fail loud, not silent.
    home2 = tempfile.mkdtemp(prefix="memnos_agents_")
    rc, out = run(home2, "agent-setup", "omnigent")
    check("omnigent: fails loud with no --agent-dir and no ~/.omnigent/config.yaml",
          rc != 0 and "agent-dir" in out)

    # --- Bug: agent-setup always minted a brand-new token via a direct Postgres connection
    # (_ensure_claude_token -> Control.mint_token), ignoring a MEMNOS_TOKEN already present
    # in the environment. That breaks the documented central/team-server workflow
    # (docs/guides/team.md): a developer exports MEMNOS_URL + MEMNOS_TOKEN and runs
    # `memnos agent-setup <agent>` expecting their admin-issued token to be wired in — the
    # old code silently discarded it and tried to mint against a DSN the remote client may
    # not even be able to reach. Fixed: an env MEMNOS_TOKEN is now used verbatim and
    # Postgres is never touched. Point MEMNOS_DSN at an unreachable address below to PROVE
    # no DB connection is attempted — if the fix regresses to minting, these would fail
    # loud (connection error) instead of writing the preset token.
    UNREACHABLE_DSN = "postgresql://nouser:nopass@127.0.0.1:1/nodb"
    PRESET_TOKEN = "mnk_PRESET_REMOTE_TOKEN_FOR_TEST"
    REMOTE_ENV = {"MEMNOS_DSN": UNREACHABLE_DSN,
                  "MEMNOS_URL": "https://memnos.example.internal:8900",
                  "MEMNOS_TOKEN": PRESET_TOKEN}

    cur_remote = os.path.join(home, ".cursor", "mcp.json")
    with open(cur_remote, "w") as f:
        json.dump({"mcpServers": {}}, f)
    rc, out = run(home, "agent-setup", "cursor", extra_env=REMOTE_ENV)
    check("MEMNOS_TOKEN env: agent-setup exits 0 against an unreachable DSN (no DB touched)",
          rc == 0)
    cr = json.load(open(cur_remote)).get("mcpServers", {}).get("memnos", {}).get("env", {})
    check("MEMNOS_TOKEN env: preset token is used verbatim in the generated config",
          cr.get("MEMNOS_TOKEN") == PRESET_TOKEN)

    # omnigent goes through the SAME shared helper (_ensure_claude_token) — this is the
    # immediate motivating case: an Omnigent host wired to a central memnos server often
    # has no direct Postgres access at all, so this integration was unusable before the fix.
    omni_remote_dir = os.path.join(home, "omni_remote_agent")
    os.makedirs(omni_remote_dir, exist_ok=True)
    omni_remote_cfg = os.path.join(omni_remote_dir, "config.yaml")
    with open(omni_remote_cfg, "w") as f:
        yaml.safe_dump({"spec_version": 1, "name": "omni_remote_agent"}, f)
    rc, out = run(home, "agent-setup", "omnigent", "--agent-dir", omni_remote_dir,
                  extra_env=REMOTE_ENV)
    check("omnigent + MEMNOS_TOKEN env: exits 0 against an unreachable DSN (no DB touched)",
          rc == 0)
    ot = yaml.safe_load(open(omni_remote_cfg)).get("tools", {}).get("memnos", {}).get("env", {})
    check("omnigent + MEMNOS_TOKEN env: preset token is used verbatim in the generated config",
          ot.get("MEMNOS_TOKEN") == PRESET_TOKEN)

    # claude-code is the HEADLINE case: it's the exact command docs/guides/team.md:140 tells
    # a developer to run after exporting MEMNOS_URL/MEMNOS_TOKEN. cmd_claude_setup does more
    # than call _ensure_claude_token, though — it also self-heals cfg's local admin_token via
    # its OWN independent _conn(cfg) call, which must be skipped too or this exact documented
    # command still crashes remotely even with the shared-helper fix in place. Needs a FRESH
    # home (no pre-existing ~/.memnos/config.json admin_token) to actually exercise that path.
    home3 = tempfile.mkdtemp(prefix="memnos_agents_")
    os.makedirs(os.path.join(home3, ".claude"), exist_ok=True)
    rc, out = run(home3, "agent-setup", "claude-code", extra_env=REMOTE_ENV)
    check("claude-code + MEMNOS_TOKEN env: exits 0 against an unreachable DSN (no DB touched)",
          rc == 0)
    cc_cfg = json.load(open(os.path.join(home3, ".claude.json")))
    cc_env = cc_cfg.get("mcpServers", {}).get("memnos", {}).get("env", {})
    check("claude-code + MEMNOS_TOKEN env: preset token is used verbatim in the MCP config",
          cc_env.get("MEMNOS_TOKEN") == PRESET_TOKEN)
    cc_settings = json.load(open(os.path.join(home3, ".claude", "settings.json")))
    check("claude-code + MEMNOS_TOKEN env: hooks still wired (recall/remember) despite no DB",
          "memnos hook recall" in json.dumps(cc_settings.get("hooks", {}))
          and "memnos hook remember" in json.dumps(cc_settings.get("hooks", {})))
    memnos_cfg_path = os.path.join(home3, ".memnos", "config.json")
    memnos_cfg = json.load(open(memnos_cfg_path)) if os.path.exists(memnos_cfg_path) else {}
    check("claude-code + MEMNOS_TOKEN env: no local admin_token minted (no DB access to mint it)",
          not memnos_cfg.get("admin_token"))

    # Regression guard: with NO MEMNOS_TOKEN in the env, the pre-existing local-mint-via-
    # Postgres behavior is unchanged — a fresh mnk_ token is minted against the real DSN.
    wind_local = os.path.join(home, ".codeium", "windsurf", "mcp_config.json")
    os.makedirs(os.path.dirname(wind_local), exist_ok=True)
    with open(wind_local, "w") as f:
        json.dump({"mcpServers": {}}, f)
    rc, out = run(home, "agent-setup", "windsurf",
                  extra_env={"MEMNOS_TOKEN": None, "MEMNOS_URL": None})
    check("no MEMNOS_TOKEN: agent-setup exits 0 (local-mint path unchanged)", rc == 0)
    wl = json.load(open(wind_local)).get("mcpServers", {}).get("memnos", {}).get("env", {})
    check("no MEMNOS_TOKEN: a fresh token is minted via Postgres (still starts with mnk_)",
          wl.get("MEMNOS_TOKEN", "").startswith("mnk_"))
    check("no MEMNOS_TOKEN: minted token differs from the unrelated preset token above",
          wl.get("MEMNOS_TOKEN") != PRESET_TOKEN)

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
