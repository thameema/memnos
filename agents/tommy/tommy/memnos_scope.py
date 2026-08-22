"""
Dispatch-scoped memnos config (issue #136).

Problem: `tommy_dispatch` (mcp_server.py) and the interactive CLI launch path
(cli.py._launch_harness) both spawn a `claude` subprocess. That subprocess
is a full Claude Code session in its own right — this environment's
auto-recall/auto-save hooks (~/.claude/settings.json's UserPromptSubmit /
Stop entries, wired by `memnos claude-setup` — see memnos_cli.py's
cmd_claude_setup) and the memnos MCP server entry (~/.claude.json's
mcpServers.memnos) are both AMBIENT, HOST-LEVEL config. Neither is aware of
tommy.yaml's `memnos.namespace` or TommyConfig.default_ns — they resolve
their own target namespace independently, via nsresolve.resolve_with_source()
(repo root's nsresolve.py), using whatever `MEMNOS_NS`/binding the AMBIENT
host config points at. A `Popen(env=...)` override on the harness process
does NOT reach the hooks: `wire()` in cmd_claude_setup bakes
`MEMNOS_URL=... MEMNOS_TOKEN=... MEMNOS_NS=... memnos hook recall` as a
literal shell `VAR=value` PREFIX on the hook command string — that prefix
always shadows the child shell's inherited env for that one command,
regardless of what the parent `claude` process's own env contains.

Fix, scoped narrowly (see module-level "Scope" section below): when the
dispatch workspace has NO EXISTING memnos binding for it (nothing in
~/.memnos/bindings_cache.json or the legacy ~/.memnos/ns_overrides.json
would resolve a namespace for this repo/host/path) AND tommy.yaml
explicitly sets `memnos.namespace`, generate a project-scoped `.mcp.json`
and `.claude/settings.local.json` in the workspace before launching, with
real MEMNOS_URL/MEMNOS_TOKEN/MEMNOS_NS values injected only into the
subprocess's `Popen` env — never written to disk as literals, only as
`${VAR}` placeholders (see generate_scoping_files()). The launched `claude`
is additionally invoked with `--setting-sources project,local` (excludes
the ambient ~/.claude/settings.json's USER-scope hooks; see
discovery.harnesses.apply_setting_sources), so the project-scoped files
above are what its own hooks and MCP tool calls actually use.

Both empirical claims this design depends on were verified against a real
`claude` 2.1.x binary before this module was written (see the issue #136 PR
description for the exact commands/markers used):
  1. A project-scoped `.mcp.json` server IS loaded and actually callable
     under Claude Code's IMPLICIT non-interactive print mode (triggered by
     `stdout=subprocess.PIPE` alone, no `-p` flag — see harnesses.py's
     memnos#132 comment) — no hang, with or without
     --dangerously-skip-permissions. WITHOUT --dangerously-skip-permissions,
     though, the tool call is silently auto-DENIED rather than prompted —
     which would look identical to "the model chose not to call memnos" in
     tommy_status's output, a much worse failure than a visible hang. Fixed
     by generate_scoping_files() also writing
     `permissions.allow: ["mcp__memnos"]` into the generated
     settings.local.json — verified this grants the tool without
     --dangerously-skip-permissions.
  2. `--setting-sources project,local` DOES exclude the user-scope
     ~/.claude/settings.json (confirmed: a marker hook wired only at
     user-scope did not fire) while still loading project-scope
     (.claude/settings.json) and local-scope (.claude/settings.local.json)
     hooks (confirmed: both fired independently and together).

Scope — what this deliberately does NOT do: override an EXISTING repo/host
binding. If `~/.memnos/bindings_cache.json` (or the legacy
ns_overrides.json) already has an entry for this workspace, the ambient
hooks/MCP config already resolve correctly for it via nsresolve's own
existing precedence (binding_repo / binding_host_repo / binding_host_path /
legacy all outrank env var and default-derived resolution — see
nsresolve.py's resolve_with_source() docstring) — same as what a human's own
interactive `claude` session in that same directory would get. Generating
scoping files in that case would not fix anything; it would just add
surface area. has_existing_binding() below replicates ONLY those four
tiers (2-4 in nsresolve's numbering) — never nsresolve's tier 1 ("explicit"
— a call-time argument shape with no static analogue here) or tier 5/6
("env"/"default" — precisely the UNBOUND cases this fix targets).

Why replicated instead of imported: `nsresolve` ships as a top-level module
of the `memnos` PyPI distribution (see repo-root pyproject.toml's
py-modules); `tommy-orchestrator` is a SEPARATE PyPI distribution (see
agents/tommy/pyproject.toml) that depends on `memnos-sdk`, not `memnos`
itself — importing `memnos.nsresolve` here would add a hard dependency on
memnos's own dependency closure (psycopg, openai, fastembed, ...) just to
read a small, stable local JSON cache file. nsresolve.py's own cache-reading
tiers are pure stdlib (json/os/re/socket/subprocess, no network) — small
enough to replicate deliberately, the same "small local copy, not a
cross-module import" call already made elsewhere in this codebase (see
mcp_server.py's _drift_git()/_clamp_commits() docstrings for the same
reasoning applied to a different pair of modules). This DOES create a
format-drift risk if nsresolve.py's bindings_cache.json shape ever changes
without a matching update here — see
agents/tommy/tests/test_memnos_scope.py's parity tests, which import the
real nsresolve.py directly (repo-root-only, dev/test environment only,
never at runtime) and assert this module's has_existing_binding() and
nsresolve.resolve_with_source()'s BOUND_SOURCES agree on synthetic cache
fixtures, specifically so drift becomes a red test instead of a silent
behavior change.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .project_config import TommyYamlError, find_tommy_yaml, load_tommy_yaml

_MEMNOS_DIR = Path.home() / ".memnos"
_BINDINGS_CACHE = _MEMNOS_DIR / "bindings_cache.json"
_NS_OVERRIDES = _MEMNOS_DIR / "ns_overrides.json"

# The memnos MCP server name this module always writes under (must match the
# permissions.allow entry below, "mcp__memnos", and cmd_claude_setup's own
# ~/.claude.json mcpServers.memnos entry name).
_MCP_SERVER_NAME = "memnos"

# Passed to discovery.harnesses.apply_setting_sources() whenever
# should_scope_dispatch() is active — excludes the ambient USER-scope
# ~/.claude/settings.json while still loading the project-scoped files this
# module generates. See module docstring point 2 for the empirical
# verification behind this exact value.
DISPATCH_SCOPE_SETTING_SOURCES = "project,local"


# ---------------------------------------------------------------------------
# nsresolve.py tiers 2-4 replication (existence check only — see module
# docstring "Why replicated instead of imported").
# ---------------------------------------------------------------------------


def _run_git(args: list[str], cwd) -> Optional[str]:
    try:
        out = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=3
        ).stdout.strip()
        return out or None
    except Exception:
        return None


def _git_root(cwd) -> Optional[str]:
    return _run_git(["rev-parse", "--show-toplevel"], cwd)


def _normalize_remote(url: str) -> Optional[str]:
    """Byte-identical port of nsresolve.py's `_normalize_remote` — see
    test_memnos_scope.py::test_normalize_remote_matches_nsresolve, which
    fuzzes both implementations against the same inputs and fails if they
    ever diverge."""
    u = url.strip()
    if u.startswith("git@"):                       # git@github.com:org/repo.git
        u = u[len("git@"):].replace(":", "/", 1)
    else:                                           # https:// | http:// | ssh:// | git://
        u = re.sub(r"^[a-z0-9+.\-]+://", "", u, flags=re.I)
        if "@" in u.split("/", 1)[0]:               # strip user@ credentials in host part
            u = u.split("@", 1)[1]
    if u.endswith(".git"):
        u = u[:-len(".git")]
    return u.rstrip("/").lower() or None


def _repo_key(cwd) -> Optional[str]:
    url = _run_git(["remote", "get-url", "origin"], cwd)
    if not url:
        return None
    return _normalize_remote(url)


def _machine_id() -> str:
    """Read-only variant of nsresolve.machine_id() — sanitize(hostname), same
    regex. Deliberately does NOT write ~/.memnos/machine_id the way
    nsresolve's version does (that's a convenience cache for ITS callers;
    this module only ever needs the value, never owns that file)."""
    host = socket.gethostname() or "unknown"
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", host.lower())).strip("-") or "unknown"


def _load_json(path: Path) -> Optional[dict]:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def has_existing_binding(ws_path: Path) -> bool:
    """True iff nsresolve.resolve_with_source() would resolve a namespace
    for `ws_path` from an actual persisted binding — tiers 2-4 in its
    numbering (binding_repo, binding_host_repo, binding_host_path, legacy
    ns_overrides.json) — NOT tier 5 (env) or tier 6 (default), which are
    exactly the two "unbound" outcomes this issue's fix targets, and NOT
    tier 1 (explicit), a call-time argument shape with no static analogue
    when merely checking whether a binding exists ahead of a dispatch.

    Never raises: a missing/corrupt cache file, no git repo, or no `git`
    on PATH all resolve to False (no binding found) — same fail-open
    posture as nsresolve.py's own resolve_with_source(), which never
    raises either."""
    cwd = str(ws_path)
    rkey = _repo_key(cwd)
    mid = _machine_id()
    cache = _load_json(_BINDINGS_CACHE) or {}
    binds = cache.get("bindings") or []

    if rkey:
        for b in binds:
            if b.get("key_type") == "repo" and (b.get("key") or "").lower() == rkey:
                return True
        for b in binds:
            if (b.get("key_type") == "host_repo" and b.get("host_id") == mid
                    and (b.get("key") or "").lower() == rkey):
                return True

    abspath = os.path.realpath(cwd)
    for b in binds:
        if (b.get("key_type") == "host_path" and b.get("host_id") == mid
                and os.path.realpath(b.get("key") or "") == abspath):
            return True

    overrides = _load_json(_NS_OVERRIDES) or {}
    root = _git_root(cwd)
    for k in (cwd, os.path.realpath(cwd), root):
        if k and overrides.get(k):
            return True

    return False


# ---------------------------------------------------------------------------
# tommy.yaml explicit-namespace check
# ---------------------------------------------------------------------------


def explicit_yaml_namespace(ws_path: Path) -> Optional[str]:
    """The workspace's tommy.yaml `memnos.namespace`, ONLY if it is actually
    present and non-empty in the file — deliberately NOT
    effective_config.resolve_effective_config()'s `.value("namespace")`,
    which always returns a value (falls back to TommyConfig.default_ns) and
    so can never express "no explicit intent was stated." A dispatch with
    no explicit tommy.yaml namespace must not have scope invented for it —
    see module docstring.

    A tommy.yaml that fails to parse degrades to None (no explicit
    namespace), the same "broken yaml -> treat as absent, don't block"
    posture secrets.py's collect_secret_refs() already documents for this
    exact failure mode (see its docstring) — this module's caller
    (tommy_dispatch / _launch_harness) already has its own, separate,
    visible surfacing of a broken tommy.yaml via the corpus-gate block;
    duplicating that here would just be a second, differently-worded
    warning for the same underlying event."""
    yaml_path = find_tommy_yaml(ws_path)
    if yaml_path is None:
        return None
    try:
        yaml_cfg = load_tommy_yaml(yaml_path)
    except TommyYamlError:
        return None
    ns = yaml_cfg.memnos.namespace
    return ns if ns else None


def should_scope_dispatch(ws_path: Path) -> tuple[bool, Optional[str]]:
    """(True, namespace) iff this dispatch should get project-scoped memnos
    config: tommy.yaml explicitly sets memnos.namespace AND no existing
    binding already covers `ws_path`. (False, None) otherwise — including
    when memnos_binary() later turns out to be unavailable (checked
    separately by the caller, since that's an environment fact independent
    of tommy.yaml/binding state, and the caller may want to log why)."""
    ns = explicit_yaml_namespace(ws_path)
    if not ns:
        return False, None
    if has_existing_binding(ws_path):
        return False, None
    return True, ns


def memnos_binary() -> Optional[str]:
    """Absolute path to `memnos` on PATH, or None. The generated .mcp.json /
    settings.local.json both invoke `memnos` by bare name (matching
    cmd_claude_setup's own non-GUI wiring) — if it's not resolvable at all,
    there is nothing for the scoping files to point at, and dispatch must
    proceed unscoped (same behavior as before this fix) rather than
    generate files that can only fail."""
    return shutil.which("memnos")


# ---------------------------------------------------------------------------
# Scoping file generation / cleanup
# ---------------------------------------------------------------------------

_ENV_PREFIX = "MEMNOS_URL=${MEMNOS_URL} MEMNOS_TOKEN=${MEMNOS_TOKEN} MEMNOS_NS=${MEMNOS_NS}"


def _mcp_server_block() -> dict:
    # ${VAR} here is Claude Code's OWN config-time substitution (its docs:
    # .mcp.json env values support ${VAR}/${VAR:-default}), resolved by the
    # `claude` PROCESS ITSELF against its own env when it parses this file
    # at startup — so this only works because the real values are injected
    # into that process's Popen env by the caller (tommy_dispatch /
    # _launch_harness), never because this file carries them itself.
    return {
        "command": "memnos",
        "args": ["mcp"],
        "env": {"MEMNOS_URL": "${MEMNOS_URL}", "MEMNOS_TOKEN": "${MEMNOS_TOKEN}", "MEMNOS_NS": "${MEMNOS_NS}"},
    }


def _write_mcp_json(path: Path) -> tuple[bool, Optional[bytes]]:
    """Merge the memnos server entry into `path`'s mcpServers, preserving
    any OTHER servers/keys already there (a repo may already commit a
    .mcp.json for unrelated MCP servers — clobbering it would be a worse
    bug than the one this module fixes). Returns (existed, prior_bytes) for
    ScopingFiles to restore verbatim on cleanup."""
    existed = path.exists()
    prior = path.read_bytes() if existed else None
    data: dict = {}
    if prior is not None:
        try:
            loaded = json.loads(prior)
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            data = {}   # unparseable pre-existing file — do not propagate garbage forward
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    servers[_MCP_SERVER_NAME] = _mcp_server_block()
    data["mcpServers"] = servers
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    return existed, prior


def _write_settings_local(path: Path) -> tuple[bool, Optional[bytes]]:
    """Merge memnos's UserPromptSubmit/Stop hooks + an mcp__memnos
    permissions.allow entry into `path`, preserving any OTHER hooks/
    settings already there. Idempotent: re-running replaces only groups
    that already look like a memnos hook (same "memnos hook" substring
    dedupe cmd_claude_setup's own wire() uses for ~/.claude/settings.json),
    same pattern, different file. Returns (existed, prior_bytes)."""
    existed = path.exists()
    prior = path.read_bytes() if existed else None
    data: dict = {}
    if prior is not None:
        try:
            loaded = json.loads(prior)
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            data = {}

    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}

    def wire(event: str, cmd: str) -> None:
        groups = [g for g in hooks.get(event, []) if "memnos hook" not in json.dumps(g)]
        groups.append({"hooks": [{"type": "command", "command": cmd, "timeout": 15}]})
        hooks[event] = groups

    wire("UserPromptSubmit", f"{_ENV_PREFIX} memnos hook recall")
    wire("Stop", f"{_ENV_PREFIX} memnos hook remember")
    data["hooks"] = hooks

    # Grants the memnos MCP server's tools without requiring
    # --dangerously-skip-permissions (verified empirically — see module
    # docstring point 1: without this, a denied tool call is silent and
    # indistinguishable from "the model chose not to call memnos").
    perms = data.get("permissions")
    if not isinstance(perms, dict):
        perms = {}
    allow = perms.get("allow")
    if not isinstance(allow, list):
        allow = []
    if f"mcp__{_MCP_SERVER_NAME}" not in allow:
        allow.append(f"mcp__{_MCP_SERVER_NAME}")
    perms["allow"] = allow
    data["permissions"] = perms

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    return existed, prior


@dataclass
class ScopingFiles:
    """Handle for the two files generate_scoping_files() wrote, so the
    caller can restore the workspace to its pre-dispatch state once the
    harness process exits. Cleanup is the default (not "leave them
    behind") specifically BECAUSE these files may be MERGES into
    pre-existing committed files, not always fresh ones — leaving a merged
    file behind would silently rewrite the user's own .mcp.json/
    settings.local.json content order/formatting even though only the
    memnos entry was ever meant to be temporary. Restoring exact prior
    bytes (not just "delete our additions") avoids drifting either file's
    formatting on every dispatch."""
    mcp_json_path: Path
    settings_local_path: Path
    mcp_json_existed: bool
    settings_local_existed: bool
    mcp_json_prior: Optional[bytes] = None
    settings_local_prior: Optional[bytes] = None
    _cleaned: bool = field(default=False, repr=False)

    def cleanup(self) -> None:
        """Best-effort, idempotent, never raises. If the harness process is
        killed abnormally (SIGKILL, host crash) before this runs, the
        generated files may be left behind — that is inert, not a leak:
        every value they carry is a `${VAR}` placeholder, never a real
        secret (see module docstring point 1), so a stale copy left in a
        workspace cannot expose anything by itself. A subsequent dispatch
        into the same workspace re-merges over it the same way either
        way."""
        if self._cleaned:
            return
        self._cleaned = True
        for path, existed, prior in (
            (self.mcp_json_path, self.mcp_json_existed, self.mcp_json_prior),
            (self.settings_local_path, self.settings_local_existed, self.settings_local_prior),
        ):
            try:
                if existed:
                    path.write_bytes(prior or b"")
                else:
                    path.unlink(missing_ok=True)
            except OSError:
                pass


def generate_scoping_files(ws_path: Path) -> ScopingFiles:
    """Write the project-scoped `.mcp.json` and `.claude/settings.local.json`
    into `ws_path` (merging into anything already there — see
    _write_mcp_json/_write_settings_local docstrings). Only ever called
    after should_scope_dispatch() has returned True; callers are
    responsible for injecting real MEMNOS_URL/MEMNOS_TOKEN/MEMNOS_NS into
    the harness subprocess's Popen env (never into these files) and for
    calling the returned ScopingFiles.cleanup() once the harness process
    exits."""
    mcp_path = ws_path / ".mcp.json"
    settings_path = ws_path / ".claude" / "settings.local.json"
    mcp_existed, mcp_prior = _write_mcp_json(mcp_path)
    settings_existed, settings_prior = _write_settings_local(settings_path)
    return ScopingFiles(
        mcp_json_path=mcp_path,
        settings_local_path=settings_path,
        mcp_json_existed=mcp_existed,
        settings_local_existed=settings_existed,
        mcp_json_prior=mcp_prior,
        settings_local_prior=settings_prior,
    )
