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

Concurrency safety (fix for a blocking finding from an adversarial review of
this module, post-#136 landing): the original generate/cleanup design had
each ScopingFiles instance snapshot the file's prior bytes independently, in
its own process memory, at its own generate_scoping_files() call, and write
those bytes back verbatim at its own cleanup() call, with no coordination
between concurrent dispatches into the SAME workspace. Tommy's own default
operating mode dispatches a wave of several concurrent subagents into one
workspace (core.md's wave-based fan-out, DEFAULT_WAVE_LIMIT), so two
overlapping generate+cleanup cycles are not an edge case here — they are the
common case. The old design's race: dispatch A generates first (snapshot =
true original O), dispatch B generates second (snapshot = A's already-merged
output O+memnos, since B reads the live file at ITS OWN generate time); if A
cleans up first (restores O, correctly but prematurely — B is still relying
on the scoped config) and B cleans up last (restores ITS OWN stale snapshot,
O+memnos), the memnos block is PERMANENTLY spliced into the workspace's real,
often git-tracked .mcp.json — not a stale leftover file, a corrupted
already-committed one. Reproduced exactly this way by the review.

Fix: reference-counted, lock-serialized, hash-verified shared state per
workspace, persisted under ~/.memnos/tommy_scope/ (same directory family as
_BINDINGS_CACHE/_NS_OVERRIDES above — local host state, never shipped with
the repo). See _acquire_holder()/_release_holder() below for the exact
mechanics; short version:
  - One shared snapshot per workspace, taken ONCE by whichever dispatch is
    first to start scoping it (an fcntl.flock'd critical section spanning
    both the "is anyone already scoping this workspace" check and the
    snapshot itself makes "first" well-defined even across two genuinely
    concurrent processes, not just concurrent threads in one).
  - The snapshot is only ever restored once the LAST live holder releases it
    (refcounting), not by whichever holder happens to call cleanup() last in
    wall-clock time.
  - Every restore — whether the ordinary last-holder-releases path, or the
    self-healing path below — first verifies the file's current on-disk
    bytes still hash-match exactly what THIS module last wrote to it. If
    they don't (a human hand-edited the file while a dispatch was scoping
    it, or between an abandoned scope and a later one that inherited its
    snapshot), the restore is skipped and the recorded snapshot is discarded
    rather than clobbering content this module didn't write. This closes a
    second, more subtle version of the same corruption class that a naive
    "just add a refcount" fix would otherwise reintroduce: without the hash
    check, a snapshot surviving across process crashes (see below) could
    overwrite a legitimate concurrent hand-edit made after the crash.
  - Self-healing for abnormal exits: a holder's liveness is tracked by the
    PID of the process that acquired it (NOT the harness subprocess — the
    tommy/tommy-mcp-server process that called generate_scoping_files()
    itself). If that PID is no longer alive, the holder is reaped. If
    reaping empties the holder set, the still-pending snapshot from the
    abandoned scope is verified and restored (or discarded, per the hash
    check above) right there, before the new caller takes its own fresh
    snapshot to become the new first holder. This means a crash that skips
    cleanup() entirely (SIGKILL, host crash, or — the daemon-thread case
    unique to mcp_server.py's tommy_dispatch — the interpreter exiting
    before a daemon thread's `finally` ever runs, which happens on ANY
    process exit, not just a crash) does not require its own code path to
    recover: the very next dispatch into that workspace heals it. Until that
    next dispatch happens, the workspace is left in the scoped
    (original-plus-memnos-block) state, never a torn/partial write (all
    writes to the workspace's own files go through the flock'd critical
    section) — the sanctioned fallback for exit paths nothing can reliably
    hook (a true SIGKILL cannot run any Python code, ours included).
  - The one abnormal-exit path that CAN be hooked reliably — SIGTERM to the
    interactive CLI process itself (cli.py's _launch_harness): Python's
    default SIGTERM disposition terminates the process without running
    `finally` blocks at all, so pre-fix, only KeyboardInterrupt (Ctrl-C) was
    ever caught there. cli.py now installs a scoped SIGTERM handler around
    proc.wait() that converts SIGTERM into a catchable exception so the
    SAME finally block (ctrl.close, prompt-file unlink, ScopingFiles.cleanup)
    that already handles KeyboardInterrupt runs for SIGTERM too — see
    _launch_harness() for the exact handler. mcp_server.py's tommy_dispatch
    deliberately does NOT get an equivalent SIGTERM handler around its own
    process: its cleanup runs on a daemon thread whose `finally` is already
    unreliable on ordinary interpreter exit for the reason above, so a
    signal handler racing arbitrary FastMCP/anyio internals would add a new,
    untestable failure surface for no reliability gain over the self-healing
    path already described.
"""
from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import time
import uuid
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


def _write_mcp_json(path: Path) -> bytes:
    """Merge the memnos server entry into `path`'s mcpServers, preserving
    any OTHER servers/keys already there (a repo may already commit a
    .mcp.json for unrelated MCP servers — clobbering it would be a worse
    bug than the one this module fixes). Returns the exact bytes written —
    the caller (_acquire_holder) hashes them for later verify-before-restore
    (see module docstring's "Concurrency safety" section); snapshotting the
    PRIOR content is a separate, shared-state concern handled there, not
    here (this function has no opinion on whether it's being called by the
    first concurrent holder or the fifth)."""
    prior = path.read_bytes() if path.exists() else None
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
    out = (json.dumps(data, indent=2) + "\n").encode()
    path.write_bytes(out)
    return out


def _write_settings_local(path: Path) -> bytes:
    """Merge memnos's UserPromptSubmit/Stop hooks + an mcp__memnos
    permissions.allow entry into `path`, preserving any OTHER hooks/
    settings already there. Idempotent: re-running replaces only groups
    that already look like a memnos hook (same "memnos hook" substring
    dedupe cmd_claude_setup's own wire() uses for ~/.claude/settings.json),
    same pattern, different file. Returns the exact bytes written — see
    _write_mcp_json's docstring for why prior-content snapshotting is
    deliberately NOT this function's job."""
    prior = path.read_bytes() if path.exists() else None
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
    out = (json.dumps(data, indent=2) + "\n").encode()
    path.write_bytes(out)
    return out


_SLOT_WRITERS = {"mcp": _write_mcp_json, "settings": _write_settings_local}


# ---------------------------------------------------------------------------
# Shared, cross-process scope state (concurrency fix — see module docstring's
# "Concurrency safety" section for the full design rationale).
#
# One JSON file + one lockfile per workspace, keyed by a hash of the
# workspace's resolved absolute path, living under ~/.memnos/tommy_scope/ —
# the same local-host-state directory family as _BINDINGS_CACHE/
# _NS_OVERRIDES above, deliberately NOT inside the workspace itself (a
# permanent, always-present bookkeeping file colocated with .mcp.json/
# settings.local.json would reintroduce a milder version of the exact
# "leaves an unwanted permanent artifact in the workspace" complaint this
# fix exists to close — and .claude/, unlike settings.local.json alone, is
# not reliably gitignored).
# ---------------------------------------------------------------------------

_SCOPE_STATE_DIR = _MEMNOS_DIR / "tommy_scope"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _current_sha(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    return _sha256(path.read_bytes())


def _workspace_state_paths(ws_path: Path) -> tuple[Path, Path]:
    """(lock_path, state_path) for `ws_path`."""
    _SCOPE_STATE_DIR.mkdir(parents=True, exist_ok=True)
    key = _sha256(os.path.realpath(str(ws_path)).encode())[:24]
    return _SCOPE_STATE_DIR / f"{key}.lock", _SCOPE_STATE_DIR / f"{key}.json"


@contextlib.contextmanager
def _locked_state(ws_path: Path):
    """Exclusive-locks `ws_path`'s scope state for the duration of the
    `with` block, yielding its state_path. fcntl.flock() locks are held per
    OPEN FILE DESCRIPTION, not per process — concurrent generate()/
    cleanup() calls from two different THREADS of the same process (exactly
    what mcp_server.py's tommy_dispatch does: each concurrent dispatch runs
    its own drain/cleanup on its own daemon thread) each open their own fd
    here and still serialize against each other correctly, the same as two
    entirely separate OS processes would. The lockfile itself is never
    deleted (a stable, empty, harmless artifact — see module docstring for
    why an occasional small residual file is an accepted, sanctioned
    trade-off here, unlike the corrupted-content bug this replaces)."""
    lock_path, state_path = _workspace_state_paths(ws_path)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield state_path
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _read_state(state_path: Path) -> dict:
    try:
        loaded = json.loads(state_path.read_text())
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}   # missing, corrupt, or partially-written — treat as "no state"


def _write_state(state_path: Path, state: dict) -> None:
    """Atomic write (temp file + os.replace) — a state file torn by a crash
    mid-write would otherwise be indistinguishable from genuine corruption
    and force a hash-mismatch abandon on the NEXT caller for a workspace
    that's actually fine; os.replace() is atomic on the same filesystem, so
    a reader only ever sees the fully-old or fully-new content, never a
    partial write."""
    tmp = state_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state))
    os.replace(str(tmp), str(state_path))


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except Exception:
        return True   # PermissionError (exists, not ours) or anything ambiguous -> assume alive
    return True   # os.kill(pid, 0) raised nothing -> the pid exists and is ours or signalable


def _read_existing(path: Path) -> tuple[bool, Optional[bytes]]:
    existed = path.exists()
    return existed, (path.read_bytes() if existed else None)


def _resolve_abandoned_snapshot(state: dict, mcp_path: Path, settings_path: Path) -> None:
    """With no live holders remaining for `state`, either restore its
    recorded pre-scope snapshot for each managed file — but ONLY if that
    file's current on-disk bytes still hash-match exactly what this module
    last wrote to it — or leave the file untouched and let the snapshot be
    discarded. The hash check is load-bearing, not defensive-programming
    boilerplate: without it, a snapshot that survives a crash (this
    function's other caller, _acquire_holder(), is exactly that "healing"
    path) could silently overwrite a legitimate edit a human made to the
    file AFTER the crash and before the next dispatch — the same class of
    bug this whole fix exists to close, just relocated. See module
    docstring's "Concurrency safety" section, point on verify-before-
    restore. Pure filesystem side effect; never mutates `state` or raises."""
    for slot, path in (("mcp", mcp_path), ("settings", settings_path)):
        expected_sha = state.get(f"{slot}_written_sha256")
        if expected_sha is None or _current_sha(path) != expected_sha:
            continue   # nothing recorded, or something else modified it since — leave it alone
        try:
            if state.get(f"{slot}_existed", False):
                prior_b64 = state.get(f"{slot}_prior_b64")
                path.write_bytes(base64.b64decode(prior_b64) if prior_b64 is not None else b"")
            else:
                path.unlink(missing_ok=True)
        except OSError:
            pass


def _fresh_snapshot(mcp_path: Path, settings_path: Path) -> dict:
    """A brand-new state dict recording each managed file's CURRENT
    (pre-scope) content as the snapshot to restore to later. Called only
    once per "scope session" — by whichever holder is first to acquire (see
    _acquire_holder()) — never by a holder that finds the workspace already
    scoped by a still-live concurrent dispatch."""
    state: dict = {"holders": {}}
    for slot, path in (("mcp", mcp_path), ("settings", settings_path)):
        existed, prior = _read_existing(path)
        state[f"{slot}_existed"] = existed
        state[f"{slot}_prior_b64"] = base64.b64encode(prior).decode() if prior is not None else None
        state[f"{slot}_written_sha256"] = None   # filled in by the caller right after writing
    return state


def _acquire_holder(ws_path: Path, holder_id: str, mcp_path: Path, settings_path: Path) -> None:
    with _locked_state(ws_path) as state_path:
        state = _read_state(state_path)
        holders = {
            hid: h for hid, h in (state.get("holders") or {}).items()
            if _pid_alive(h.get("pid", -1))
        }

        if not holders:
            # No live holder: either the true first-ever dispatch to scope
            # this workspace, or every previous holder crashed/exited
            # without releasing (self-healing case — see module docstring).
            # Resolve any leftover snapshot from an abandoned scope BEFORE
            # taking the fresh one below, so the fresh snapshot reflects
            # genuine pre-dispatch content rather than a leftover merge.
            _resolve_abandoned_snapshot(state, mcp_path, settings_path)
            state = _fresh_snapshot(mcp_path, settings_path)

        for slot, path in (("mcp", mcp_path), ("settings", settings_path)):
            written = _SLOT_WRITERS[slot](path)
            state[f"{slot}_written_sha256"] = _sha256(written)

        holders[holder_id] = {"pid": os.getpid(), "started": time.time()}
        state["holders"] = holders
        _write_state(state_path, state)


def _release_holder(ws_path: Path, holder_id: str, mcp_path: Path, settings_path: Path) -> None:
    with _locked_state(ws_path) as state_path:
        state = _read_state(state_path)
        holders = state.get("holders") or {}
        holders.pop(holder_id, None)
        holders = {hid: h for hid, h in holders.items() if _pid_alive(h.get("pid", -1))}

        if holders:
            # Other dispatches are still (live-)using this workspace's
            # scoped config — do NOT restore yet. The snapshot taken by
            # whichever holder was first stays exactly as recorded.
            state["holders"] = holders
            _write_state(state_path, state)
            return

        # We are the last live holder (or every remaining one had already
        # crashed) — restore now, verifying first (see
        # _resolve_abandoned_snapshot's docstring), then clear the state
        # entirely so the NEXT dispatch starts a fresh scope session.
        _resolve_abandoned_snapshot(state, mcp_path, settings_path)
        try:
            state_path.unlink(missing_ok=True)
        except OSError:
            pass


@dataclass
class ScopingFiles:
    """Handle for one dispatch's hold on the project-scoped `.mcp.json` /
    `.claude/settings.local.json` generate_scoping_files() wrote into
    `ws_path`. Restoring the workspace to its pre-dispatch state is a
    SHARED responsibility across every concurrent ScopingFiles instance for
    the same workspace, not something any single instance can decide on its
    own — see _acquire_holder()/_release_holder() and the module docstring's
    "Concurrency safety" section for the full mechanics (reference-counted,
    lock-serialized, hash-verified-before-restore, self-healing across
    abnormal exits)."""
    mcp_json_path: Path
    settings_local_path: Path
    ws_path: Path
    holder_id: str
    _cleaned: bool = field(default=False, repr=False)

    def cleanup(self) -> None:
        """Best-effort, idempotent, never raises. Releases this dispatch's
        hold on the workspace's shared scope state; the workspace is only
        actually restored once every OTHER concurrent hold on it has also
        been released (or self-healed away — see module docstring)."""
        if self._cleaned:
            return
        self._cleaned = True
        try:
            _release_holder(self.ws_path, self.holder_id, self.mcp_json_path, self.settings_local_path)
        except Exception:
            pass


def generate_scoping_files(ws_path: Path) -> ScopingFiles:
    """Write the project-scoped `.mcp.json` and `.claude/settings.local.json`
    into `ws_path` (merging into anything already there — see
    _write_mcp_json/_write_settings_local docstrings), registering this call
    as a new holder of `ws_path`'s shared scope state (see
    _acquire_holder()). Only ever called after should_scope_dispatch() has
    returned True; callers are responsible for injecting real
    MEMNOS_URL/MEMNOS_TOKEN/MEMNOS_NS into the harness subprocess's Popen
    env (never into these files) and for calling the returned
    ScopingFiles.cleanup() once the harness process exits."""
    mcp_path = ws_path / ".mcp.json"
    settings_path = ws_path / ".claude" / "settings.local.json"
    holder_id = f"{os.getpid()}:{uuid.uuid4().hex}"
    _acquire_holder(ws_path, holder_id, mcp_path, settings_path)
    return ScopingFiles(
        mcp_json_path=mcp_path,
        settings_local_path=settings_path,
        ws_path=ws_path,
        holder_id=holder_id,
    )
