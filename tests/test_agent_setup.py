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

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable
PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += cond; FAIL += (not cond)


def run(home, *args):
    env = dict(os.environ, MEMNOS_DSN=DSN, HOME=home)
    try:
        for line in open(os.path.join(ROOT, ".env")):
            if line.strip().startswith("MEMNOS_SECRET_KEY=") and "MEMNOS_SECRET_KEY" not in os.environ:
                env["MEMNOS_SECRET_KEY"] = line.strip().split("=", 1)[1]
    except FileNotFoundError:
        pass
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

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
