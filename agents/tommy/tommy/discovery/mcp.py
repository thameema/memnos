"""MCP config reader + optional stdio introspection."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Optional

from .. import __version__


# Common Claude Code MCP config locations
_CLAUDE_MCP_PATHS = [
    Path.home() / ".claude" / "claude_desktop_config.json",
    Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json",
    Path("/etc/claude/mcp_servers.json"),
]


def read_mcp_servers(config_paths: Optional[list[str]] = None) -> dict:
    """Read MCP server definitions from Claude config JSON files."""
    paths = [Path(p) for p in config_paths] if config_paths else _CLAUDE_MCP_PATHS
    servers: dict = {}
    for p in paths:
        if p.exists():
            try:
                data = json.loads(p.read_text())
                servers.update(data.get("mcpServers", {}))
            except Exception:
                continue
    return servers


def introspect_tools(server_name: str, server_cfg: dict, timeout: int = 5) -> list[dict]:
    """
    Attempt to introspect MCP tools via stdio JSON-RPC (initialize → tools/list).
    Returns list of tool dicts {name, description, inputSchema} or [] on failure.
    """
    cmd = server_cfg.get("command")
    args = server_cfg.get("args", [])
    env_extra = server_cfg.get("env", {})
    if not cmd:
        return []

    env = {**os.environ, **env_extra}
    full_cmd = [cmd] + args

    init_msg = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "tommy", "version": __version__},
        },
    }) + "\n"
    list_msg = json.dumps({
        "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
    }) + "\n"

    try:
        proc = subprocess.Popen(
            full_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            text=True,
        )
        stdout, _ = proc.communicate(input=init_msg + list_msg, timeout=timeout)
        proc.terminate()
    except Exception:
        return []

    tools = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            resp = json.loads(line)
        except json.JSONDecodeError:
            continue
        if resp.get("id") == 2:
            result = resp.get("result", {})
            tools = result.get("tools", [])
            break
    return tools


def format_mcp_manifest(servers: dict, introspect: bool = False) -> str:
    """
    Format the MCP servers into a manifest string for injection into the system prompt.
    When introspect=True, attempts stdio tool discovery.
    """
    if not servers:
        return ""

    lines = ["## Available MCP Servers\n"]
    for name, cfg in servers.items():
        cmd = cfg.get("command", "?")
        args = " ".join(cfg.get("args", []))
        lines.append(f"### {name}")
        lines.append(f"- command: `{cmd} {args}`.strip()`")

        env_keys = list(cfg.get("env", {}).keys())
        if env_keys:
            lines.append(f"- env: {', '.join(env_keys)}")

        if introspect:
            tools = introspect_tools(name, cfg)
            if tools:
                lines.append(f"- tools ({len(tools)}):")
                for t in tools:
                    desc = t.get("description", "")
                    desc_short = desc[:80] + "…" if len(desc) > 80 else desc
                    lines.append(f"  - `{t['name']}`: {desc_short}")
            else:
                lines.append("  - *(tool introspection unavailable)*")
        lines.append("")

    return "\n".join(lines)
