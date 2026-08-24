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



# Harnesses that accept --dangerously-skip-permissions
_SUPPORTS_SKIP_PERMISSIONS = {"claude"}


def apply_skip_permissions(cmd: list[str], harness_name: str, skip: bool) -> list[str]:
    """Prepend --dangerously-skip-permissions to cmd for supported harnesses."""
    if skip and harness_name in _SUPPORTS_SKIP_PERMISSIONS:
        # Insert right after the binary name
        return [cmd[0], "--dangerously-skip-permissions"] + cmd[1:]
    return cmd



_SUPPORTS_SESSION_NAME = {"claude"}


def apply_session_name(cmd: list[str], harness_name: str, name: str) -> list[str]:
    """Inject --name <name> right after the binary for supported harnesses."""
    if harness_name in _SUPPORTS_SESSION_NAME and name:
        return [cmd[0], "--name", name] + cmd[1:]
    return cmd


# Bug (memnos#132): claude auto-enters non-interactive "print mode" whenever
# its own stdout is not a TTY (true for every tommy_dispatch launch, and for
# the interactive CLI path whenever ITS stdout is piped/redirected too), and
# print mode hard-requires an actual prompt via stdin or a positional
# argument. `launch_template` only ever carries
# `--append-system-prompt-file {prompt_file}` — system-level context, never
# a "prompt" in claude's own CLI sense — so every non-interactive launch
# failed immediately with "Error: Input must be provided either through
# stdin or as a prompt argument when using --print", and no dispatched task
# ever actually reached Claude Code. `--append-system-prompt-file` is kept
# exactly as-is (the fused core.md + task content still arrives as *system*
# context, not a literal user turn) — this only adds the minimal trailing
# trigger claude's CLI itself requires to leave print-mode's input gate.
#
# Harness-scoped (like _SUPPORTS_SKIP_PERMISSIONS/_SUPPORTS_SESSION_NAME
# above): only "claude" is known to have this exact contract; other
# harnesses aren't installed/verified on this machine and are out of scope
# for this fix (see memnos#132).
_NEEDS_TRAILING_PROMPT_ARG = {"claude"}

DISPATCH_TRIGGER_PROMPT = (
    "Begin. Follow the instructions and the task described in the system "
    "prompt above."
)


_SUPPORTS_SETTING_SOURCES = {"claude"}


def apply_setting_sources(cmd: list[str], harness_name: str, sources: str) -> list[str]:
    """Insert `--setting-sources <sources>` right after the binary name for
    supported harnesses (memnos#136): used ONLY when Tommy has generated
    project-scoped memnos config for this dispatch (see
    tommy/memnos_scope.py) — excludes the harness's ambient USER-scope
    settings.json (which carries the host's own memnos hooks, unscoped by
    tommy.yaml) while still loading the project-scoped files this dispatch
    just wrote. Verified empirically against a real `claude` 2.1.x binary
    that `--setting-sources project,local` excludes user-scope
    ~/.claude/settings.json while still loading project-scope
    (.claude/settings.json) and local-scope (.claude/settings.local.json)
    — see memnos_scope.py's module docstring point 2."""
    if sources and harness_name in _SUPPORTS_SETTING_SOURCES:
        return [cmd[0], "--setting-sources", sources] + cmd[1:]
    return cmd


_SUPPORTS_SESSION_ID = {"claude"}


def supports_session_id(harness_name: str) -> bool:
    """Whether `harness_name` accepts a pre-assigned --session-id (memnos#144)
    — callers that need to decide BEFORE building `cmd` whether a
    claude_session_id is even meaningful to generate/store (e.g.
    mcp_server.py's Task.claude_session_id) should check this rather than
    duplicating the _SUPPORTS_SESSION_ID set."""
    return harness_name in _SUPPORTS_SESSION_ID


def apply_session_id(cmd: list[str], harness_name: str, session_id: str) -> list[str]:
    """Insert `--session-id <uuid>` right after the binary for supported
    harnesses (memnos#144): pre-assigns claude's own session ID rather than
    discovering it after the fact by parsing output. Verified empirically
    against a real `claude` 2.1.x binary that a pre-assigned `--session-id`
    is (a) accepted together with `--print`/`--append-system-prompt-file`/
    `--name`, and (b) genuinely resumable afterward via `claude --resume
    <that-same-uuid>` with real conversation context intact — a stronger,
    simpler guarantee than parsing `--output-format stream-json` for the
    session_id claude assigns on its own, and available immediately at
    dispatch time (no need to wait for the harness to produce any output).

    Deliberately NOT used on the interactive CLI launch path
    (cli.py's _launch_harness): also verified empirically that
    `claude --resume <id> --session-id <other-id>` (i.e. this flag combined
    with a resume/continue flag arriving via extra_args passthrough) is a
    hard error unless `--fork-session` is also given ("--session-id can
    only be used with --continue or --resume if --fork-session is also
    specified") — unconditionally injecting --session-id on that path would
    break the exact `-r`/`-c`/`--resume` passthrough this same issue
    documents. tommy_dispatch never receives a user-supplied --resume, so
    no such collision exists there.
    """
    if session_id and harness_name in _SUPPORTS_SESSION_ID:
        return [cmd[0], "--session-id", session_id] + cmd[1:]
    return cmd


def apply_prompt_arg(cmd: list[str], harness_name: str, trigger: str) -> list[str]:
    """Append `trigger` as a trailing positional prompt argument for
    harnesses whose CLI requires one to leave non-interactive print mode
    (see memnos#132). Appended at the END of cmd — claude's own grammar is
    `claude [options] [prompt]`, so this must come after every flag/value
    pair already in cmd, never spliced in the middle like
    apply_skip_permissions/apply_session_name do at the front.

    No-op for harnesses not in _NEEDS_TRAILING_PROMPT_ARG, or when `trigger`
    is falsy (callers that already have a real prompt from elsewhere, e.g.
    cli.py's `extra_args`, should simply not call this rather than pass an
    empty trigger)."""
    if trigger and harness_name in _NEEDS_TRAILING_PROMPT_ARG:
        return cmd + [trigger]
    return cmd


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
