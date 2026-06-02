"""
memnos install — detect and configure AI coding tools to use memnos via MCP.

Supported tools
---------------
  claude-code   ~/.claude/settings.json          (mcpServers block)
  cursor        ~/.cursor/mcp.json
  windsurf      ~/.codeium/windsurf/mcp_config.json
  zed           ~/.config/zed/settings.json       (context_servers block — HTTP)

Usage
-----
  memnos-install --detect              # auto-detect and install all found tools
  memnos-install --tool cursor         # install only one tool
  memnos-install --list                # show what was detected, don't write
  memnos-install --dry-run             # show what would change, don't write
  memnos-install --url http://...      # custom server URL (default: localhost:8765)
  memnos-install --key <api-key>       # API key (default: memnos-local-dev-key)
  memnos-install --uninstall           # remove memnos from all detected configs
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

def _home(*parts: str) -> Path:
    return Path.home().joinpath(*parts)


TOOLS: dict[str, dict] = {
    "claude-code": {
        "label": "Claude Code",
        "detect": lambda: _home(".claude").exists() or bool(shutil.which("claude")),
        "config_path": _home(".claude", "settings.json"),
        "format": "claude-code",   # {mcpServers: {memnos: {...}}}
    },
    "cursor": {
        "label": "Cursor",
        "detect": lambda: _home(".cursor").exists() or bool(shutil.which("cursor")),
        "config_path": _home(".cursor", "mcp.json"),
        "format": "mcp-standard",   # {mcpServers: {memnos: {...}}}
    },
    "windsurf": {
        "label": "Windsurf",
        "detect": lambda: _home(".codeium", "windsurf").exists() or bool(shutil.which("windsurf")),
        "config_path": _home(".codeium", "windsurf", "mcp_config.json"),
        "format": "mcp-standard",
    },
    "zed": {
        "label": "Zed",
        "detect": lambda: _home(".config", "zed").exists() or bool(shutil.which("zed")),
        "config_path": _home(".config", "zed", "settings.json"),
        "format": "zed",            # {context_servers: {memnos: {url: ..., token: ...}}}
    },
}

UNSUPPORTED_TOOLS = {
    "github-copilot": "Copilot does not support MCP directly. Use Copilot Extensions (REST) to call memnos at http://localhost:8766.",
    "aider": "Aider has no MCP support. Use: aider --read <(memnos-export --ns your:ns) to inject memories.",
    "codex": "Codex CLI has no MCP support. Set CODEX_INSTRUCTIONS to inject memnos context manually.",
}

# ---------------------------------------------------------------------------
# MCP entry builders
# ---------------------------------------------------------------------------

def _sse_entry(url: str, api_key: str) -> dict:
    return {"type": "sse", "url": url, "headers": {"Authorization": f"Bearer {api_key}"}}


def _merge_claude_code(config: dict, url: str, api_key: str, tool_key: str = "memnos") -> tuple[dict, bool]:
    config.setdefault("mcpServers", {})
    changed = config["mcpServers"].get(tool_key) != _sse_entry(url, api_key)
    config["mcpServers"][tool_key] = _sse_entry(url, api_key)
    return config, changed


def _merge_mcp_standard(config: dict, url: str, api_key: str, tool_key: str = "memnos") -> tuple[dict, bool]:
    config.setdefault("mcpServers", {})
    changed = config["mcpServers"].get(tool_key) != _sse_entry(url, api_key)
    config["mcpServers"][tool_key] = _sse_entry(url, api_key)
    return config, changed


def _merge_zed(config: dict, url: str, api_key: str, tool_key: str = "memnos") -> tuple[dict, bool]:
    # Zed uses context_servers with a simpler shape
    config.setdefault("context_servers", {})
    entry = {"url": url.replace("/sse", ""), "token": api_key}   # Zed HTTP, not SSE
    changed = config["context_servers"].get(tool_key) != entry
    config["context_servers"][tool_key] = entry
    return config, changed


_MERGERS = {
    "claude-code": _merge_claude_code,
    "mcp-standard": _merge_mcp_standard,
    "zed": _merge_zed,
}

# ---------------------------------------------------------------------------
# Install / uninstall helpers
# ---------------------------------------------------------------------------

def _read_config(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            print(f"  ⚠ {path} contains invalid JSON — will merge into empty config", file=sys.stderr)
    return {}


def _write_config(path: Path, config: dict, dry_run: bool) -> None:
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n")


def _install_tool(name: str, meta: dict, url: str, api_key: str, dry_run: bool) -> bool:
    path = meta["config_path"]
    fmt = meta["format"]
    config = _read_config(path)
    merger = _MERGERS[fmt]
    config, changed = merger(config, url, api_key)

    if dry_run:
        status = "would update" if changed else "already correct"
    else:
        status = "updated" if changed else "already correct"
    print(f"  {meta['label']:14} {path}  [{status}]")

    if not dry_run and changed:
        _write_config(path, config, dry_run=False)
    return changed


def _uninstall_tool(name: str, meta: dict, dry_run: bool) -> bool:
    path = meta["config_path"]
    if not path.exists():
        print(f"  {meta['label']:14} not configured — skip")
        return False

    config = _read_config(path)
    fmt = meta["format"]

    if fmt in ("claude-code", "mcp-standard"):
        removed = config.get("mcpServers", {}).pop("memnos", None) is not None
    elif fmt == "zed":
        removed = config.get("context_servers", {}).pop("memnos", None) is not None
    else:
        removed = False

    status = "would remove" if dry_run else "removed" if removed else "not found"
    print(f"  {meta['label']:14} {path}  [{status}]")

    if not dry_run and removed:
        _write_config(path, config, dry_run=False)
    return removed

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="memnos-install",
        description="Wire memnos MCP server into AI coding tools (Claude Code, Cursor, Windsurf, Zed).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--detect", action="store_true", default=False,
        help="Auto-detect installed tools and configure them (default action).",
    )
    parser.add_argument(
        "--tool", metavar="NAME",
        help=f"Install only a specific tool. Choices: {', '.join(TOOLS)}",
    )
    parser.add_argument(
        "--list", action="store_true", dest="list_only",
        help="List detected tools without modifying any config.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would change without writing any files.",
    )
    parser.add_argument(
        "--uninstall", action="store_true",
        help="Remove memnos from all detected (or --tool specified) configs.",
    )
    parser.add_argument(
        "--url", default="http://localhost:8765/sse", metavar="URL",
        help="memnos MCP SSE URL (default: http://localhost:8765/sse).",
    )
    parser.add_argument(
        "--key", default=os.environ.get("MEMNOS_API_KEY", "memnos-local-dev-key"),
        metavar="API_KEY",
        help="API key for memnos (default: $MEMNOS_API_KEY or memnos-local-dev-key).",
    )
    parser.add_argument(
        "--all-tools", action="store_true",
        help="Install into all known tools even if not detected.",
    )

    args = parser.parse_args()

    # ── Determine which tools to act on ──────────────────────────────────────
    if args.tool:
        if args.tool not in TOOLS:
            if args.tool in UNSUPPORTED_TOOLS:
                print(f"✗ {args.tool}: {UNSUPPORTED_TOOLS[args.tool]}")
            else:
                print(f"✗ Unknown tool '{args.tool}'. Choices: {', '.join(TOOLS)}")
            sys.exit(1)
        targets = {args.tool: TOOLS[args.tool]}
    elif args.all_tools:
        targets = TOOLS
    else:
        targets = {k: v for k, v in TOOLS.items() if v["detect"]()}

    # ── List mode ────────────────────────────────────────────────────────────
    if args.list_only:
        print("Detected tools:")
        for name, meta in TOOLS.items():
            found = meta["detect"]()
            cfg = meta["config_path"]
            configured = _is_configured(cfg, meta["format"])
            markers = []
            if found:
                markers.append("installed")
            if configured:
                markers.append("memnos configured")
            label = ", ".join(markers) if markers else "not detected"
            print(f"  {'✓' if found else '·'} {meta['label']:14} [{label}]")
        print()
        print("Unsupported (no MCP):")
        for name, msg in UNSUPPORTED_TOOLS.items():
            print(f"  · {name}: {msg}")
        return

    if not targets:
        print("No supported AI coding tools detected.")
        print(f"Try: memnos-install --tool {{{'|'.join(TOOLS)}}} or --all-tools")
        sys.exit(0)

    # ── Install / Uninstall ───────────────────────────────────────────────────
    action = "Uninstalling" if args.uninstall else "Installing"
    suffix = " (dry run)" if args.dry_run else ""
    print(f"{action} memnos MCP{suffix}:")
    print(f"  URL: {args.url}")
    if not args.uninstall:
        print(f"  Key: {args.key[:8]}{'*' * (len(args.key) - 8) if len(args.key) > 8 else ''}")
    print()

    changed_count = 0
    for name, meta in targets.items():
        if args.uninstall:
            changed = _uninstall_tool(name, meta, dry_run=args.dry_run)
        else:
            changed = _install_tool(name, meta, args.url, args.key, dry_run=args.dry_run)
        if changed:
            changed_count += 1

    print()
    if args.dry_run:
        print(f"{changed_count} config(s) would be updated. Run without --dry-run to apply.")
    else:
        print(f"{changed_count} config(s) updated.")
        if not args.uninstall:
            print()
            print("Restart each tool to load the new MCP server.")
            print("In Claude Code: /mcp to verify · In Cursor: Ctrl+Shift+P → MCP")


def _is_configured(path: Path, fmt: str) -> bool:
    if not path.exists():
        return False
    try:
        config = json.loads(path.read_text())
        if fmt in ("claude-code", "mcp-standard"):
            return "memnos" in config.get("mcpServers", {})
        if fmt == "zed":
            return "memnos" in config.get("context_servers", {})
    except Exception:
        pass
    return False


if __name__ == "__main__":
    main()
