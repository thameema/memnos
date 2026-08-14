"""Harness registry — detect what LLM CLIs are on PATH."""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class HarnessSpec:
    name: str
    binary: str
    launch_template: list[str]           # {prompt_file} replaced at runtime
    supports_tools: bool = True
    supports_mcp: bool = False
    description: str = ""
    available: bool = False


# Core harness registry — extended by user TOML drop-ins
HARNESS_REGISTRY: dict[str, HarnessSpec] = {
    "claude": HarnessSpec(
        name="claude",
        binary="claude",
        launch_template=["claude", "--append-system-prompt-file", "{prompt_file}"],
        supports_tools=True,
        supports_mcp=True,
        description="Anthropic Claude Code — full tool + MCP support, 200K context",
    ),
    "codex": HarnessSpec(
        name="codex",
        binary="codex",
        launch_template=["codex", "--full-auto", "--system-prompt-file", "{prompt_file}"],
        supports_tools=True,
        supports_mcp=False,
        description="OpenAI Codex CLI — diff-as-deliverable, single-file focus",
    ),
    "cursor-agent": HarnessSpec(
        name="cursor-agent",
        binary="cursor-agent",
        launch_template=["cursor-agent", "--system", "{prompt_file}"],
        supports_tools=True,
        supports_mcp=True,
        description="Cursor IDE agent — IDE-integrated, use when user requests",
    ),
    "hermes": HarnessSpec(
        name="hermes",
        binary="hermes",
        launch_template=["hermes", "--system-file", "{prompt_file}"],
        supports_tools=False,
        supports_mcp=False,
        description="Hermes (local llama.cpp) — zero data egress, PHI-safe",
    ),
    "aider": HarnessSpec(
        name="aider",
        binary="aider",
        launch_template=["aider", "--system-prompt", "{prompt_file}", "--no-auto-commits"],
        supports_tools=False,
        supports_mcp=False,
        description="Aider — autonomous coding, longer unattended runs",
    ),
    "goose": HarnessSpec(
        name="goose",
        binary="goose",
        launch_template=["goose", "run", "--system-prompt-file", "{prompt_file}"],
        supports_tools=True,
        supports_mcp=True,
        description="Goose (Block) — autonomous coding agent",
    ),
    "kiro": HarnessSpec(
        name="kiro",
        binary="kiro",
        launch_template=["kiro", "--system", "{prompt_file}"],
        supports_tools=True,
        supports_mcp=False,
        description="Kiro (Amazon) — IDE-integrated, use when user requests",
    ),
}


def discover() -> dict[str, HarnessSpec]:
    """Return a copy of the registry with `available` set based on PATH."""
    result = {}
    for name, spec in HARNESS_REGISTRY.items():
        s = HarnessSpec(**spec.__dict__)
        s.available = shutil.which(spec.binary) is not None
        result[name] = s
    return result


def load_user_harnesses(harnesses_dir: Optional[Path] = None) -> dict[str, HarnessSpec]:
    """Load TOML drop-in harness definitions from ~/.memnos/harnesses/."""
    import sys
    result: dict[str, HarnessSpec] = {}
    d = harnesses_dir or (Path.home() / ".memnos" / "harnesses")
    if not d.exists():
        return result

    if sys.version_info >= (3, 11):
        import tomllib
    else:
        try:
            import tomllib  # type: ignore
        except ImportError:
            try:
                import tomli as tomllib  # type: ignore
            except ImportError:
                return result

    for toml_file in sorted(d.glob("*.toml")):
        try:
            data = tomllib.loads(toml_file.read_text())
        except Exception:
            continue
        for name, cfg in data.items():
            result[name] = HarnessSpec(
                name=name,
                binary=cfg.get("binary", name),
                launch_template=cfg.get("launch_template", [name, "{prompt_file}"]),
                supports_tools=cfg.get("supports_tools", False),
                supports_mcp=cfg.get("supports_mcp", False),
                description=cfg.get("description", f"User-defined harness: {name}"),
                available=shutil.which(cfg.get("binary", name)) is not None,
            )
    return result


def all_harnesses(extra_dir: Optional[Path] = None) -> dict[str, HarnessSpec]:
    """Merge built-in registry (with availability) + user TOML drop-ins."""
    merged = discover()
    merged.update(load_user_harnesses(extra_dir))
    return merged
