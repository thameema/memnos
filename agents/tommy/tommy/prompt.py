"""
Prompt layer stacker for Tommy.

Layer order (each layer appended):
  1. core.md          — bundled brain (Tommy's identity, rules, memnos usage)
  2. {org}.md         — org-specific overrides (optional)
  3. projects/{key}.md — project context (optional, only when project selected)
  4. .tommy.md        — workspace-local overrides (optional, cwd/.tommy.md)
  5. Runtime config block — harness list, project info, config values
  6. MCP manifest     — available MCP servers/tools
  7. Dispatched task  — optional; the specific work item for a headless run
                         (tommy_dispatch only — the interactive CLI path never
                         passes this; a human types the task into the live
                         harness session instead)

build_prompt() is the single loader for this whole stack. Both Tommy entry
points call it: the interactive `tommy` CLI (tommy.cli._launch_harness) and
the `tommy_dispatch` MCP tool (tommy.mcp_server.tommy_dispatch) — the latter
via the `task` parameter below, so a harness launched over MCP gets the same
core.md coordinator contract as one launched from a terminal, not a
reimplementation of a subset of it.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from .config import TommyConfig, ProjectEntry
from .discovery.harnesses import all_harnesses
from .discovery.mcp import read_mcp_servers, format_mcp_manifest


def _read_optional(path: Path) -> str:
    if path.exists():
        return path.read_text().strip()
    return ""


def _layer(title: str, content: str) -> str:
    if not content.strip():
        return ""
    return f"\n\n---\n<!-- {title} -->\n{content}"


def build_prompt(
    cfg: TommyConfig,
    project_key: Optional[str] = None,
    task: Optional[str] = None,
) -> str:
    """
    Build the full layered system prompt (see module docstring for layer order).

    `task`, when given, appends the specific work item for a headless dispatch
    as a final layer (see layer 7). The interactive CLI path never passes it —
    build_prompt(cfg, project_key=...) there is exactly what it's always been.
    """
    layers: list[str] = []

    prompts_dir = cfg.prompts_dir

    # 1. core.md
    core_path = prompts_dir / "core.md"
    if not core_path.exists():
        # fallback to bundled
        core_path = Path(__file__).parent / "prompts" / "core.md"
    core = _read_optional(core_path)
    if core:
        layers.append(core)

    # 2. org.md
    org_md = _read_optional(prompts_dir / f"{cfg.org}.md")
    if org_md:
        layers.append(_layer(f"org:{cfg.org}", org_md))

    # 3. project layer
    project: Optional[ProjectEntry] = None
    if project_key:
        project = cfg.project_by_key(project_key)
        if project:
            proj_md = _read_optional(prompts_dir / "projects" / f"{project.key}.md")
            if proj_md:
                layers.append(_layer(f"project:{project.key}", proj_md))

    # 4. workspace-local .tommy.md
    local_md = _read_optional(Path.cwd() / ".tommy.md")
    if local_md:
        layers.append(_layer("workspace-local", local_md))

    # 5. Runtime config block
    harnesses = all_harnesses()
    available = [name for name, spec in harnesses.items() if spec.available]
    unavailable = [name for name, spec in harnesses.items() if not spec.available]

    harness_lines = []
    for name in available:
        spec = harnesses[name]
        tool_tag = "tools+MCP" if spec.supports_mcp else ("tools" if spec.supports_tools else "no-tools")
        harness_lines.append(f"  - **{name}** ({tool_tag}): {spec.description}")

    project_info = ""
    if project:
        project_info = (
            f"\nActive project: **{project.name}** "
            f"(key={project.key}, JIRA={project.jira_project}, "
            f"git_root={project.git_root})"
        )
    elif cfg.projects:
        keys = ", ".join(p.key for p in cfg.projects)
        project_info = f"\nKnown projects: {keys}  (select with --project <key>)"

    runtime_block = f"""## Runtime Configuration

- User: {cfg.tommy_user}
- Org: {cfg.org}
- TOMMY_NS: {cfg.tommy_ns}
- DEFAULT_NS: {cfg.default_ns}
- Default model: {cfg.default_model}
- SMART_ROUTING: {"on" if cfg.smart_routing else "off"}
- Active harness: {cfg.harness}
{project_info}

### Available harnesses (detected on PATH)
{chr(10).join(harness_lines) if harness_lines else "  (none detected — install claude or another supported harness)"}

### Unavailable harnesses
  {", ".join(unavailable) if unavailable else "(none)"}
"""

    layers.append(_layer("runtime-config", runtime_block))

    # 6. MCP manifest
    mcp_servers = read_mcp_servers()
    mcp_manifest = format_mcp_manifest(mcp_servers, introspect=cfg.mcp_introspect)
    if mcp_manifest:
        layers.append(_layer("mcp-manifest", mcp_manifest))

    # 7. Dispatched task (tommy_dispatch only) — core.md above assumes an
    # interactive session (it opens by printing a greeting and permits asking
    # the user a clarifying question). A headless dispatch has no human turn
    # to answer either one, so this layer overrides both before handing over
    # the actual task.
    if task:
        dispatch_note = (
            "**Non-interactive dispatch.** This session was launched headlessly "
            "via `tommy_dispatch` — there is no human at the terminal to answer "
            "a question or read a greeting. Do not print the session-start "
            "greeting instructed above, and do not ask a clarifying "
            "question; if the task is ambiguous, make the most reasonable "
            "assumption, state it, and proceed. Every other rule above "
            "(dispatch-first, spawn bounds, leases, corpus_check, wave-based "
            "fan-out, memnos journaling) still applies.\n\n"
            f"## Task\n\n{task}"
        )
        layers.append(_layer("dispatched-task", dispatch_note))

    return "\n".join(layers)
