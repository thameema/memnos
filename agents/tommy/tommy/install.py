"""
Tommy first-time installer.

tommy --install
  - Writes ~/.memnos/agents/tommy/tommy.conf (if not present)
  - Writes ~/.claude/commands/memnos*.md (always, idempotent)
"""
from __future__ import annotations

import shutil
from pathlib import Path


_BUNDLED_CONF = Path(__file__).parent / "tommy.conf.default"
_USER_CONF = Path.home() / ".memnos" / "agents" / "tommy" / "tommy.conf"
_SLASH_SRC = Path(__file__).parent / "slash_commands"
_SLASH_DST = Path.home() / ".claude" / "commands"
_BUNDLED_PROMPTS = Path(__file__).parent / "prompts"
_USER_PROMPTS_DIR = Path.home() / ".memnos" / "agents" / "tommy" / "prompts"


def install_config(force: bool = False) -> None:
    """Write tommy.conf to ~/.memnos/agents/tommy/ if it doesn't exist (or force=True)."""
    _USER_CONF.parent.mkdir(parents=True, exist_ok=True)
    if _USER_CONF.exists() and not force:
        print(f"  ✓ Config already exists: {_USER_CONF}")
        print("    Run with --force to overwrite.")
        return
    if _BUNDLED_CONF.exists():
        shutil.copy2(_BUNDLED_CONF, _USER_CONF)
        print(f"  ✓ Config written: {_USER_CONF}")
        print("    → Edit TOMMY_USER, ORG, PROJECTS to match your setup.")
    else:
        print(f"  ✗ Bundled config not found: {_BUNDLED_CONF}")


def install_slash_commands(force: bool = False) -> None:
    """Copy every *.md Claude Code slash command file (e.g. /memnos-recall,
    /sketch) from the bundled slash_commands/ directory to ~/.claude/commands/."""
    if not _SLASH_SRC.exists():
        print(f"  ✗ Slash commands directory not found: {_SLASH_SRC}")
        return
    _SLASH_DST.mkdir(parents=True, exist_ok=True)
    for src_file in sorted(_SLASH_SRC.glob("*.md")):
        dst_file = _SLASH_DST / src_file.name
        if dst_file.exists() and not force:
            print(f"  ✓ Already installed: {dst_file.name}")
            continue
        shutil.copy2(src_file, dst_file)
        print(f"  ✓ Installed: {dst_file}")


def install_prompts(force: bool = False) -> None:
    """Copy bundled prompts to ~/.memnos/agents/tommy/prompts/ if not present."""
    if not _BUNDLED_PROMPTS.exists():
        return
    _USER_PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    for src_file in sorted(_BUNDLED_PROMPTS.rglob("*.md")):
        rel = src_file.relative_to(_BUNDLED_PROMPTS)
        dst_file = _USER_PROMPTS_DIR / rel
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        if dst_file.exists() and not force:
            print(f"  ✓ Prompt already exists: {dst_file.name}")
            continue
        shutil.copy2(src_file, dst_file)
        print(f"  ✓ Prompt installed: {rel}")


def run_install(force: bool = False) -> None:
    print("\n🟣 Tommy installer\n")
    print("── Config ──")
    install_config(force=force)
    print("\n── Slash commands ──")
    install_slash_commands(force=force)
    print("\n── Prompts ──")
    install_prompts(force=force)
    print("\n✅ Done.")
    print(f"   Edit your config: {_USER_CONF}")
    print("   Then run: tommy")
