"""
Tommy CLI entrypoint.

Usage:
    tommy                           # launch with default harness
    tommy --project myapp           # activate project context
    tommy --install                 # first-time setup
    tommy --list-projects           # show configured projects
    tommy --list-harnesses          # show detected harnesses
    tommy --conf /path/to/conf      # explicit config file
    tommy --force                   # combined with --install: overwrite existing
    tommy --version                 # print the installed version and exit
    tommy config show               # print fully-resolved effective config
    tommy generate                  # write/update harness adapters from tommy.yaml

Resuming a previous session (memnos#144):
    Tommy's `main()` command is deliberately permissive about its own
    argv — `context_settings={"ignore_unknown_options": True,
    "allow_extra_args": True}` plus a trailing
    `@click.argument("extra_args", nargs=-1, type=click.UNPROCESSED)` means
    any flag Tommy itself doesn't recognize is passed straight through,
    verbatim, to the end of the launched harness's own command line
    (`_launch_harness`). For the "claude" harness this means every one of
    claude's own resume flags already works today, with no Tommy-specific
    resume command needed:

        tommy --resume <session-id>     # resume that exact claude session
        tommy --resume                  # claude's own interactive picker
        tommy -c                        # / tommy --continue — resume the
                                         #   most recent session IN THIS
                                         #   PROJECT'S DIRECTORY (Tommy
                                         #   launches the harness with
                                         #   cwd=<project git_root>, so
                                         #   claude's own "most recent
                                         #   conversation in the current
                                         #   directory" semantics scope
                                         #   correctly per-project already)
        tommy --resume "<session title>"  # resume by title — Tommy gives
                                         #   every launch a unique title
                                         #   ("Tommy | PROJECT | <run-id>"),
                                         #   so this disambiguates correctly
                                         #   even across multiple prior
                                         #   sessions for the same project

    This is pure passthrough: run `claude --help` for the authoritative,
    up-to-date list of resume-related flags (-r/--resume, -c/--continue,
    --session-id, --fork-session, -n/--name, ...) — Tommy does not
    reimplement, wrap, or validate any of them.

    Separately, tommy_dispatch (the MCP tool, headless/non-interactive) has
    no resume capability of its own: every dispatch is a brand-new claude
    session. A completed dispatch's own session ID is captured as
    Task.claude_session_id and surfaced via tommy_status, so it CAN be
    resumed manually afterward with a real `claude --resume
    <claude_session_id>` — but tommy_dispatch itself has no `resume=`
    parameter in this pass; see issue #144's explicitly-out-of-scope note
    on a future Tommy-native, disk-persisted task history.
"""
from __future__ import annotations

import fnmatch
import glob
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional

import click

from . import __version__
from .config import TommyConfig
from .corpus import corpus_ingest as _http_corpus_ingest, corpus_list as _http_corpus_list
from .effective_config import EffectiveConfig, resolve_effective_config
from .project_config import TommyYamlError
from .prompt import build_prompt
from .install import run_install
from .mcp_server import run_stdio
from .control import ControlServer
from .discovery.harnesses import (
    all_harnesses,
    apply_prompt_arg,
    apply_session_name,
    apply_setting_sources,
    apply_skip_permissions,
    DISPATCH_TRIGGER_PROMPT,
)
from .generate_cmd import config_group, generate_command
from .memnos_scope import (
    DISPATCH_SCOPE_SETTING_SOURCES,
    generate_scoping_files,
    memnos_binary,
    should_scope_dispatch,
)
from .secrets import (
    SecretResolutionError,
    collect_secret_refs,
    resolve_secret_env,
    secret_resolve_client,
)


# ---------------------------------------------------------------------------
# memnos helpers
# ---------------------------------------------------------------------------

def _memnos_client(cfg: TommyConfig):
    """Return a MemnosClient or None if memnos is unreachable."""
    try:
        from memnos_sdk import MemnosClient  # type: ignore
        client = MemnosClient(
            base_url=cfg.memnos_url,
            token=cfg.memnos_token,
            namespace=cfg.tommy_ns,
        )
        if client.healthy():
            return client
    except Exception:
        pass
    return None


def _ensure_memnos_running(cfg: TommyConfig) -> bool:
    """
    Check if memnos HTTP server is reachable.
    If not, attempt to start it in the background (best-effort).
    Returns True if memnos is usable.
    """
    import urllib.request
    import urllib.error

    def _reachable() -> bool:
        try:
            urllib.request.urlopen(f"{cfg.memnos_url}/healthz", timeout=2)
            return True
        except Exception:
            return False

    if _reachable():
        return True

    # Not reachable — try to start the daemon
    click.echo("  memnos not running — attempting auto-start...", err=True)
    try:
        from urllib.parse import urlparse as _urlparse
        _parsed = _urlparse(cfg.memnos_url)
        _port = _parsed.port or 8900
        subprocess.Popen(
            ["memnos", "start", "--http", "--port", str(_port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        # Give it 3s to come up
        for _ in range(6):
            time.sleep(0.5)
            if _reachable():
                click.echo("  memnos started ✓", err=True)
                return True
    except FileNotFoundError:
        pass  # memnos binary not on PATH

    return False


def _journal_start(client, cfg: TommyConfig, project_key: Optional[str]) -> None:
    """Write a 'session started' memory to TOMMY_NS."""
    try:
        proj_note = f", project={project_key}" if project_key else ""
        client.remember(
            f"Tommy session started (harness={cfg.harness}, model={cfg.default_model}"
            f", smart_routing={'on' if cfg.smart_routing else 'off'}{proj_note})",
            namespace=cfg.tommy_ns,
        )
    except Exception:
        pass


def _find_latest_claude_transcript() -> Optional[Path]:
    """
    Locate the most recently modified Claude Code conversation JSONL.
    Claude Code stores conversations at:
      ~/.claude/projects/<project-dir>/<uuid>.jsonl
    (No 'conversations/' subdirectory — each project dir holds .jsonl files directly.)
    """
    pattern = str(Path.home() / ".claude" / "projects" / "*" / "*.jsonl")
    files = glob.glob(pattern)
    if not files:
        return None
    return Path(max(files, key=os.path.getmtime))


def _post_run_capture(client, cfg: TommyConfig, run_id: str, project_key: Optional[str]) -> None:
    """
    After the sub-agent exits:
    1. Ingest the Claude Code conversation transcript
    2. Consolidate recent turns into durable semantic facts
    """
    try:
        # Layer 2: feed — what did the sub-agent write?
        # (subscription was created before Popen; we stored sub_id in run context)
        pass  # sub_id is threaded through via caller; nothing to do here

        # Layer 3: ingest conversation transcript
        transcript = _find_latest_claude_transcript()
        if transcript:
            text = transcript.read_text(errors="replace")
            client.ingest_file(
                filename=f"tommy-run-{run_id}.jsonl",
                text=text,
                extract=True,
            )
            click.echo(f"  📥 Transcript ingested ({len(text):,} chars) → memnos")

        # Layer 4: consolidate (extract durable facts from recent turns)
        try:
            client.consolidate()
            click.echo("  🧠 Session consolidated → memnos")
        except Exception:
            pass  # consolidate is best-effort

        # Journal completion
        proj_note = f", project={project_key}" if project_key else ""
        client.remember(
            f"Tommy session ended (run_id={run_id}{proj_note}, transcript_ingested={transcript is not None})",
            namespace=cfg.tommy_ns,
        )
    except Exception:
        pass  # capture is best-effort — never block the user


# ---------------------------------------------------------------------------
# Auto-ingest design docs into the corpus (issue #109)
# ---------------------------------------------------------------------------


def _git(repo_root: Path, *args: str) -> Optional[str]:
    """Run a git command in `repo_root`. Returns stripped stdout on success
    (exit 0), None on any failure (non-zero exit, git not on PATH, or a
    timeout) — callers treat None as "can't determine this, skip the
    auto-ingest step" rather than raising."""
    try:
        out = subprocess.run(
            ["git", *args], cwd=str(repo_root),
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def _git_diff_base(repo_root: Path) -> Optional[str]:
    """Return the commit to diff HEAD against for auto-ingest change
    detection, or None if `repo_root` isn't inside a git repo (or git isn't
    on PATH) — the caller must skip auto-ingest entirely in that case, not
    crash the CLI.

    Prefers HEAD~10 (the original scope's window). Falls back to the oldest
    commit git currently has (the true root commit — or, on a shallow
    clone, the shallow boundary commit git grafts in as if it had no
    parents) when history has fewer than 10 commits, so a fresh checkout or
    a CI shallow clone still gets *a* diff instead of a crash or a silent
    no-op. Trade-off, stated plainly: on a single-commit repo the "oldest
    commit" IS HEAD, so the diff is empty and nothing gets ingested until a
    second commit exists — a real gap for a brand-new repo's first design
    docs, accepted for v1 rather than special-cased against the empty tree.
    """
    if _git(repo_root, "rev-parse", "--is-inside-work-tree") != "true":
        return None
    verified = _git(repo_root, "rev-parse", "--verify", "--quiet", "HEAD~10")
    if verified:
        return verified
    roots = _git(repo_root, "rev-list", "--max-parents=0", "HEAD")
    if roots:
        # A history with multiple unrelated roots (rare) yields multiple
        # lines; the first is a deterministic, good-enough choice — diffing
        # against any root still surfaces every design-doc change on this
        # branch since project inception.
        return roots.splitlines()[0]
    return None


def _changed_files_since(repo_root: Path, base_ref: str) -> list[str]:
    """git diff --name-only <base_ref> HEAD, as repo-root-relative paths.
    Returns [] (not an error) if the diff itself fails for any reason."""
    diff = _git(repo_root, "diff", "--name-only", base_ref, "HEAD")
    if diff is None:
        return []
    return [line.strip() for line in diff.splitlines() if line.strip()]


def _auto_ingest_changed_docs(
    cfg: TommyConfig, effective: EffectiveConfig, client, repo_root: Path,
) -> None:
    """Ingest changed design docs into the corpus (issue #109's
    corpus.auto_ingest). Called once per `tommy` launch when auto_ingest is
    on (see main()'s call site) — deliberately launch-time rather than
    post-run-only, so THIS launch's corpus_gate checks (which run inside
    tommy_dispatch, in a separate process) see doc edits made before this
    launch rather than only the next one. See this feature's PR description
    ("Design decisions") for the full reasoning, including the asymmetry
    this creates: auto-ingest only runs on this (interactive CLI) path,
    while corpus_gate only runs on the tommy_dispatch (MCP) path — per
    issue #109's file-by-file scope, they never fire in the same process.

    Best-effort / never blocks the launch: a git failure, an unreachable
    memnos, a read-only token (403 from the WRITE_OPS-gated /corpus/ingest),
    or no design_docs glob configured are all logged to stderr and
    swallowed — the same fail-open-but-visible posture corpus_gate itself
    uses, extended here because a developer's `tommy` launch must never
    fail over corpus bookkeeping.
    """
    design_docs_globs = effective.value("design_docs")
    if not design_docs_globs:
        return  # nothing configured — quiet no-op, not a misconfiguration

    if client is None:
        click.echo("  auto_ingest: skipped (memnos unreachable)", err=True)
        return

    base_ref = _git_diff_base(repo_root)
    if base_ref is None:
        click.echo(
            f"  auto_ingest: skipped ({repo_root} is not a git repository, or git is unavailable)",
            err=True,
        )
        return

    changed = _changed_files_since(repo_root, base_ref)
    matched = sorted({
        f for f in changed
        if any(fnmatch.fnmatch(f, pattern) for pattern in design_docs_globs)
    })
    if not matched:
        return  # no design-doc changes in this window — the common case, stays quiet

    namespace = effective.value("namespace")

    # Snapshot constraint counts BEFORE re-ingesting: ingest_constraints()
    # deletes-then-reinserts a source's constraints on every re-ingest
    # (core/store.py — idempotent by design), so a doc that gets truncated,
    # emptied, or has its SHALL/MUST language reworded away would otherwise
    # silently wipe every constraint future corpus_gate checks derive from
    # it (issue #109's "silent constraint wipe" open question). We don't
    # block on this (fail-open) — just make a drop loudly visible.
    prior_counts: dict[str, int] = {}
    listing = _http_corpus_list(cfg.memnos_url, cfg.memnos_token, namespace)
    if listing.get("ok"):
        prior_counts = {s["name"]: s.get("constraint_count", 0) for s in listing.get("sources", [])}

    head_sha = _git(repo_root, "rev-parse", "HEAD")

    click.echo(
        f"  auto_ingest: {len(matched)} changed design doc(s) match design_docs — "
        f"ingesting into corpus ({namespace})",
        err=True,
    )
    for rel_path in matched:
        full_path = repo_root / rel_path
        if not full_path.is_file():
            continue  # deleted (or renamed away) within this diff window — nothing to ingest

        try:
            text = full_path.read_text(errors="replace")
        except OSError as exc:
            click.echo(f"    ⚠ {rel_path}: could not read ({exc}) — skipped", err=True)
            continue

        result = _http_corpus_ingest(
            cfg.memnos_url, cfg.memnos_token, namespace, rel_path, text,
            kind="doc", git_sha=head_sha,
        )
        if not result.get("ok"):
            click.echo(f"    ✗ {rel_path}: ingest failed — {result.get('error')}", err=True)
            continue

        new_count = result.get("constraints", 0)
        old_count = prior_counts.get(rel_path)
        if old_count and new_count == 0:
            click.echo(
                f"    ⚠ {rel_path}: re-ingest dropped constraints {old_count} -> 0 — "
                "every corpus_gate check relying on this source just lost that coverage; "
                "verify the doc still has real SHALL/MUST/SHOULD language",
                err=True,
            )
        elif old_count is not None and new_count < old_count:
            click.echo(f"    ⚠ {rel_path}: constraints {old_count} -> {new_count} (decreased)", err=True)
        else:
            click.echo(f"    ✓ {rel_path}: {new_count} constraint(s) ingested", err=True)


# ---------------------------------------------------------------------------
# Launch harness via Popen (stays alive for capture)
# ---------------------------------------------------------------------------


def _on_ctrl_message(msg: dict) -> None:
    """Handle progress / checkpoint / question messages from a running harness."""
    mtype = msg.get("type", "")
    if mtype == "progress":
        pct = msg.get("pct", "?")
        detail = msg.get("detail", "")
        click.echo(f"  ◌ {pct}% — {detail}" if detail else f"  ◌ {pct}%")
    elif mtype == "checkpoint":
        phase = msg.get("phase", "")
        summary = msg.get("summary", "")
        click.echo(f"  ✔ [{phase}] {summary}")
    elif mtype == "done":
        click.echo(f"  ✔ done — {msg.get('summary', '')}")
    elif mtype == "error":
        click.echo(f"  ✗ harness error: {msg.get('message', '')}", err=True)
    elif mtype == "question":
        text = msg.get("text", "")
        opts = msg.get("options")
        opts_str = f" ({'/'.join(opts)})" if opts else ""
        click.echo(f"  ❓ {text}{opts_str}")
    # Unknown message types are silently ignored (forward compat)



def _launch_harness(
    cfg: TommyConfig,
    project_key: Optional[str],
    extra_args: tuple,
    memnos_client=None,
) -> None:
    """Build prompt, write to temp file, Popen the harness, capture output after."""
    harnesses = all_harnesses()
    spec = harnesses.get(cfg.harness)

    if spec is None:
        click.echo(f"✗ Unknown harness '{cfg.harness}'. Known: {', '.join(harnesses)}", err=True)
        sys.exit(1)

    if not spec.available:
        click.echo(
            f"✗ Harness '{cfg.harness}' not found on PATH.\n"
            f"  Install it, or set HARNESS=<name> in your tommy.conf.\n"
            f"  Available: {', '.join(n for n, s in harnesses.items() if s.available) or 'none'}",
            err=True,
        )
        sys.exit(1)

    # Resolve working directory: project git_root > CWD. Computed here (moved
    # up from its original spot just above the Popen call below) because
    # Secret Shield's tommy.yaml discovery, right below, needs to search from
    # the harness's actual working directory, not necessarily Tommy's own
    # CWD. Pure reordering — the computation itself is unchanged.
    ws_path: Path = Path.cwd()
    if project_key:
        proj = cfg.project_by_key(project_key)
        if proj:
            candidate = Path(proj.git_root).expanduser().resolve()
            if candidate.is_dir():
                ws_path = candidate

    # ── Dispatch-scoped memnos config (issue #136) ──────────────────────────
    # Same fix as mcp_server.py's tommy_dispatch — the interactive `tommy`
    # launch path spawns the exact same kind of `claude` subprocess, with
    # the exact same ambient-config problem (see tommy/memnos_scope.py's
    # module docstring). Decided here, before Secret Shield below, since
    # ws_path is already settled and neither block depends on the other.
    _scope_active, _scope_ns = should_scope_dispatch(ws_path)
    if _scope_active and memnos_binary() is None:
        _scope_active = False  # nothing to point the generated files at
    # ─────────────────────────────────────────────────────────────────────

    # ── Secret Shield (issue #115) ────────────────────────────────────────
    # Resolve secret://NAME references BEFORE any other launch-prep work —
    # before build_prompt(), before the prompt tempfile is written, before
    # ControlServer binds a port. This is deliberately earlier than "before
    # env = os.environ.copy()" below: a written tempfile and a bound control
    # socket are both live state that a later fail-closed check would have
    # to clean up, and the original design ("resolve immediately before
    # env.copy()") was verified too late for exactly that reason (see
    # tommy/secrets.py's module docstring). Placed after the harness
    # availability check above (a free, local check) so a misconfigured
    # HARNESS fails via the cheaper path first — but before any network I/O.
    #
    # Ordering vs. issue #109's corpus-gate check, if/when #109 lands in this
    # same function: secret resolution runs before the corpus gate, and
    # after the harness-availability check above (a free, local check —
    # #109's issue text places its own gate even before that; this doesn't
    # conflict, it just means #109's gate would land between the harness
    # check and this block). Secret resolution is a hard fail-closed
    # security boundary; #109's corpus-gate is advisory/best-effort by its
    # own design (see #109's "fail-open vs fail-closed" open question). A
    # hard stop should not wait on an advisory network call whose result
    # would be discarded anyway if secret resolution is about to abort the
    # launch. See PR description "Design decisions" for the full reasoning.
    resolved_secrets: dict[str, str] = {}
    try:
        # collect_secret_refs() degrades to "no env: entries from a broken
        # tommy.yaml" rather than raising on a parse error — see its
        # docstring for why (short version: a ref that never got read isn't
        # a leak, just a missing env var; #109's auto-ingest block above
        # already surfaces "tommy.yaml could not be read" for this same
        # condition). Fail-closed here is for a ref that DID resolve to a
        # name and then failed to RESOLVE.
        secret_refs = collect_secret_refs(cfg, workspace=ws_path)
        if secret_refs:
            resolved_secrets = resolve_secret_env(secret_refs, secret_resolve_client(cfg))
    except SecretResolutionError as exc:
        click.echo(
            f"✗ Secret resolution failed — refusing to launch harness.\n  {exc}",
            err=True,
        )
        sys.exit(1)
    # ─────────────────────────────────────────────────────────────────────

    prompt = build_prompt(cfg, project_key=project_key)

    # Write prompt to temp file (deleted after harness exits)
    tf = tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", prefix="tommy-prompt-", delete=False
    )
    tf.write(prompt)
    tf.flush()
    tf.close()
    prompt_file = tf.name

    cmd = [
        part.replace("{prompt_file}", prompt_file)
        for part in spec.launch_template
    ] + list(extra_args)
    cmd = apply_skip_permissions(cmd, cfg.harness, cfg.skip_permissions)

    # memnos#144: run_id is generated here (moved up from its original spot
    # further down, just before the Control channel block) specifically so
    # the session title below can embed it. Before this, the title was
    # IDENTICAL on every launch for a given project ("Tommy | PROJECT" every
    # time) — reproduced live as a real "--resume 'Tommy | TEST' matches 2
    # sessions" hard error the moment a second prior session with that exact
    # title existed. Appending run_id makes every launch's title unique, so
    # both claude's own title-based `--resume "<title>"` and its interactive
    # `--resume` picker can actually disambiguate between a project's prior
    # sessions. Nothing else in between here and the old call site read
    # run_id before this point, so the move is a pure reorder.
    run_id = str(uuid.uuid4())[:8]
    _session_name = f"Tommy | {project_key.upper()} | {run_id}" if project_key else f"Tommy | {run_id}"
    cmd = apply_session_name(cmd, cfg.harness, _session_name)

    # memnos#132: two DIFFERENT questions, deliberately not conflated into
    # one flag — claude itself keeps them separate, and testing showed a
    # single combined check gets the wrong answer on `tommy | tee log.txt`
    # run from a real terminal (stdin IS a TTY, stdout is NOT):
    #
    #   1. Will claude auto-enter print mode and therefore require a real
    #      prompt (positional or stdin)? This is driven by claude's own
    #      STDOUT, not stdin (confirmed via `claude --help`: "...or when
    #      stdout is not a TTY, e.g. piped or redirected output"). The
    #      Popen() call below never overrides stdout, so the harness
    #      child's stdout is simply whatever Tommy's own stdout is —
    #      `sys.stdout.isatty()` here is the exact same check claude itself
    #      would apply to the fd it actually inherits.
    #   2. Should Tommy's own stdin be inherited by the harness child at
    #      all? Only when it's a real terminal — that's the assumption the
    #      SIGINT/process-group design below already makes (a human types
    #      into the launched session). When it's NOT a TTY (`tommy` invoked
    #      from a script/cron/CI with stdin piped or redirected, or a fifo)
    #      inheriting it risks this issue's stdin-inheritance bug: a live,
    #      never-closing pipe leaking into the child and never reaching EOF
    #      — verified empirically (see mcp_server.py's dispatch Popen call
    #      for the full writeup; same underlying claude CLI behavior).
    #
    # A trailing trigger prompt is appended when (1) is true UNLESS the user
    # already supplied a real prompt via extra_args (e.g.
    # `tommy "fix the bug" > log.txt`) — extra_args alone already satisfies
    # print-mode's input requirement there, and appending a second
    # positional would be wrong. `_stdin_interactive` (question 2) drives
    # ONLY the Popen `stdin=` choice below — it has no bearing on whether a
    # trigger is needed.
    _print_mode_will_trigger = not sys.stdout.isatty()
    _stdin_interactive = sys.stdin.isatty()
    if _print_mode_will_trigger and not extra_args:
        cmd = apply_prompt_arg(cmd, cfg.harness, DISPATCH_TRIGGER_PROMPT)
    if _scope_active:
        cmd = apply_setting_sources(cmd, cfg.harness, DISPATCH_SCOPE_SETTING_SOURCES)

    # Inject MEMNOS_URL so the sub-agent's MCP config picks it up
    env = os.environ.copy()
    env["MEMNOS_URL"] = cfg.memnos_url
    env["TOMMY_NS"] = cfg.tommy_ns
    env["TOMMY_DEFAULT_NS"] = cfg.default_ns
    if _scope_active:
        # See mcp_server.py's tommy_dispatch for the identical block/reasoning
        # — real values only, into the subprocess's own env, never into the
        # generated files (those carry only ${VAR} placeholders).
        env["MEMNOS_TOKEN"] = cfg.memnos_token or ""
        env["MEMNOS_NS"] = _scope_ns
    if resolved_secrets:
        # Real values only — never the secret:// reference string. Overlaid
        # exactly like every other env var above; names only in the log line
        # below, never the resolved plaintext.
        env.update(resolved_secrets)

    click.echo(f"🟣 Tommy → {cfg.harness}  (smart_routing={'on' if cfg.smart_routing else 'off'})")
    if _scope_active:
        click.echo(f"  🔒 memnos scope: dispatched session's own hooks/MCP scoped to {_scope_ns!r} "
                   "(no existing binding for this workspace — see memnos_scope.py)")
    if resolved_secrets:
        click.echo(f"  🔒 Resolved {len(resolved_secrets)} secret(s) into subprocess env: "
                   f"{', '.join(sorted(resolved_secrets))}")
    if project_key:
        proj = cfg.project_by_key(project_key)
        if proj:
            click.echo(f"   Project: {proj.name} ({proj.jira_project}) @ {proj.git_root}")
            click.echo(f"   Workspace: {proj.git_root}")

    # Note: namespace_subscribe/feed are MCP-only tools; SDK has no such methods.
    # Post-run capture uses ingest_file + consolidate instead.
    sub_id: Optional[int] = None

    # run_id generated earlier now (see the cmd/session-name construction
    # above, memnos#144) — reused here unchanged for _post_run_capture's
    # transcript-ingest journaling.

    # ── Control channel (bidirectional IPC) ──────────────────────────────────
    ctrl = ControlServer(
        on_message=_on_ctrl_message,
        connect_timeout=30.0,
    )
    env["TOMMY_CTRL_PORT"] = str(ctrl.port)
    # ─────────────────────────────────────────────────────────────────────────────

    # ── Popen: Tommy stays alive ──────────────────────────────────────────
    # ws_path was resolved earlier (Secret Shield needs it for tommy.yaml
    # discovery before build_prompt() — see above).

    # Do NOT use start_new_session=True on the interactive CLI path.
    # Tommy is the foreground process; without it the harness shares the same
    # process group so Ctrl-C (SIGINT) reaches both. With start_new_session=True
    # the harness lands in a new session, SIGINT only kills Tommy, and the harness
    # keeps running unattended — confirmed repro by the remote reviewer.
    # (mcp_server.py keeps start_new_session=True for background MCP dispatches.)
    #
    # memnos#132: stdin is explicit here, never left to Popen's implicit
    # "inherit" default — `None` (inherit the real terminal) when Tommy's
    # own stdin genuinely is one (`_stdin_interactive`, computed above),
    # DEVNULL otherwise. See the comment at cmd's construction above for why
    # this is a different check than the one deciding whether a trigger
    # prompt was needed.
    # Generated immediately before Popen, mirroring mcp_server.py's
    # tommy_dispatch — minimizes the window between writing these files and
    # the harness actually reading them.
    _scoping_files = generate_scoping_files(ws_path) if _scope_active else None

    try:
        proc = subprocess.Popen(
            cmd, env=env, cwd=str(ws_path),
            stdin=None if _stdin_interactive else subprocess.DEVNULL,
        )
    except Exception:
        # Popen itself failed — the finally block below (attached to
        # proc.wait(), never reached) can't clean these up, so this is the
        # only place left to.
        if _scoping_files is not None:
            _scoping_files.cleanup()
        raise

    try:
        proc.wait()
    except KeyboardInterrupt:
        # Ctrl-C propagated to the whole process group (Tommy + harness).
        # Wait for the harness to finish its own graceful shutdown before cleanup.
        try:
            proc.wait()
        except KeyboardInterrupt:
            pass  # second Ctrl-C: don't block further
    finally:
        # Cleanup always runs — even on KeyboardInterrupt or unexpected exceptions.
        exit_code = proc.returncode if proc.returncode is not None else 130
        ctrl.close()
        try:
            os.unlink(prompt_file)
        except OSError:
            pass
        if _scoping_files is not None:
            _scoping_files.cleanup()

    # Post-run capture (Layer 3: ingest transcript, Layer 4: consolidate)
    if memnos_client:
        _post_run_capture(memnos_client, cfg, run_id, project_key)

    sys.exit(exit_code)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _detect_installer(package: str) -> str:
    """Return 'uv', 'pip', 'pipx', or 'unknown' from the INSTALLER dist-info file."""
    import importlib.metadata
    try:
        dist = importlib.metadata.Distribution.from_name(package)
        for f in dist.files or []:
            if f.name == "INSTALLER":
                return f.read_text().strip().lower()
    except importlib.metadata.PackageNotFoundError:
        pass
    from pathlib import Path
    exe = Path(sys.executable).resolve()
    uv_root = (Path.home() / ".local" / "share" / "uv" / "tools").resolve()
    if str(exe).startswith(str(uv_root)):
        return "uv"
    return "unknown"


def _do_upgrade() -> None:
    """Upgrade Tommy using the same installer that originally installed it."""
    import shutil
    import subprocess
    from pathlib import Path

    home = Path.home()
    installer = _detect_installer("tommy-orchestrator")

    if installer == "uv":
        uv = shutil.which("uv")
        if uv:
            cmd = [uv, "tool", "upgrade", "tommy-orchestrator"]
        else:
            click.echo(
                "  [warn] INSTALLER=uv but 'uv' not on PATH. Run manually:\n"
                "         cd ~ && uv tool upgrade tommy-orchestrator",
                err=True,
            )
            sys.exit(1)
    elif installer == "pip":
        # Explicit pip install — upgrade in-place, no warning
        cmd = [sys.executable, "-m", "pip", "install", "-U", "tommy-orchestrator"]
    elif installer in ("pipx", "unknown") and shutil.which("pipx"):
        try:
            out = subprocess.run(
                ["pipx", "list", "--short"], capture_output=True, text=True, cwd=str(home),
            ).stdout
            cmd = ["pipx", "upgrade", "tommy-orchestrator"] if "tommy" in out else [
                sys.executable, "-m", "pip", "install", "-U", "tommy-orchestrator"
            ]
        except Exception:
            cmd = [sys.executable, "-m", "pip", "install", "-U", "tommy-orchestrator"]
    else:
        # installer==unknown and pipx not available — last resort
        click.echo(
            "  [warn] installer unknown, falling back to pip — if this fails, run:\n"
            "         cd ~ && uv tool upgrade tommy-orchestrator",
            err=True,
        )
        cmd = [sys.executable, "-m", "pip", "install", "-U", "tommy-orchestrator"]

    click.echo(f"🔄 Upgrading Tommy ... ({' '.join(cmd)})")
    rc = subprocess.run(cmd, cwd=str(home)).returncode
    if rc == 0:
        click.echo("✓ Tommy upgraded. Restart any running sessions.")
    else:
        click.echo(
            f"✗ Upgrade failed (exit {rc}).\n"
            "  Try manually:  cd ~ && uv tool upgrade tommy-orchestrator",
            err=True,
        )
        sys.exit(rc)


TOMMY_LOGO = r"""
  ████████╗ ██████╗ ███╗   ███╗███╗   ███╗██╗   ██╗
  ╚══██╔══╝██╔═══██╗████╗ ████║████╗ ████║╚██╗ ██╔╝
     ██║   ██║   ██║██╔████╔██║██╔████╔██║ ╚████╔╝
     ██║   ██║   ██║██║╚██╔╝██║██║╚██╔╝██║  ╚██╔╝
     ██║   ╚██████╔╝██║ ╚═╝ ██║██║ ╚═╝ ██║   ██║
     ╚═╝    ╚═════╝ ╚═╝     ╚═╝╚═╝     ╚═╝   ╚═╝
"""

def _print_banner(cfg: TommyConfig, project_key: Optional[str] = None) -> None:
    """Print the Tommy launch banner to stderr (never mixed into JSON output)."""
    import sys
    PURPLE = "\033[38;5;99m"
    BLUE   = "\033[38;5;27m"
    GREY   = "\033[38;5;245m"
    RESET  = "\033[0m"
    BOLD   = "\033[1m"

    # Only colour if stderr is a real TTY
    tty = sys.stderr.isatty()
    o = PURPLE if tty else ""
    b = BLUE   if tty else ""
    g = GREY   if tty else ""
    r = RESET  if tty else ""
    bold = BOLD if tty else ""

    logo_coloured = "\n".join(
        f"{o}{line}{r}" for line in TOMMY_LOGO.splitlines()
    )
    click.echo(logo_coloured, err=True)
    click.echo(f"  {bold}memnos-native coding orchestrator{r}  {g}v{__version__}{r}", err=True)



    click.echo("", err=True)



@click.command(
    # help_option_names=[] disables click's automatic eager --help on THIS
    # command. Without that, "tommy generate --help" would never reach the
    # dispatch below — click's built-in --help option is checked (and would
    # exit) before our function body ever runs, printing tommy's own top-level
    # help instead of generate_command's. main() re-implements --help/-h
    # itself below, after the generate/config dispatch has had first look.
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True, "help_option_names": []},
    help="Tommy — personal coding orchestrator built on memnos.",
    add_help_option=False,
    epilog=(
        "Resuming a session (memnos#144): any flag Tommy doesn't recognize "
        "is passed straight through, verbatim, to the launched harness "
        "('claude' today), so claude's own resume flags already work — see "
        "`claude --help` for the authoritative list.\n\n"
        "\b\n"
        "  tommy --resume <session-id>       resume that exact session\n"
        "  tommy --resume                    claude's interactive picker\n"
        "  tommy -c / tommy --continue       resume the most recent session\n"
        "                                    in this project's directory\n"
        "  tommy --resume \"<session title>\"  resume by title (every launch\n"
        "                                    gets a unique title, so this\n"
        "                                    disambiguates correctly)\n\n"
        "This is pure passthrough — Tommy does not reimplement or "
        "validate any of these flags."
    ),
)
@click.version_option(__version__, "-V", "--version", prog_name="tommy")
@click.option("--conf", default=None, metavar="PATH", help="Path to tommy.conf override.")
@click.option("--project", "-p", default=None, metavar="KEY", help="Activate a project context.")
@click.option("--install", "do_install", is_flag=True, help="Run first-time setup.")
@click.option("--force", is_flag=True, help="With --install: overwrite existing files.")
@click.option("--list-projects", is_flag=True, help="List configured projects.")
@click.option("--list-harnesses", is_flag=True, help="List detected harnesses.")
@click.option("--mcp", "mcp_mode", is_flag=True, help="Run as MCP stdio server (editor-managed subprocess, no daemon, no port).")
@click.option("--upgrade", "do_upgrade", is_flag=True, help="Upgrade Tommy via uv tool install (same venv, no pip).")
@click.option("--no-memnos-check", is_flag=True, help="Skip memnos health check at startup.")
@click.option("--ask-permissions", is_flag=True, help="Require manual approval for every harness tool call (overrides SKIP_PERMISSIONS=on).")
@click.argument("extra_args", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def main(
    ctx: click.Context,
    conf: Optional[str],
    project: Optional[str],
    do_install: bool,
    force: bool,
    list_projects: bool,
    list_harnesses: bool,
    mcp_mode: bool,
    do_upgrade: bool,
    no_memnos_check: bool,
    ask_permissions: bool,
    extra_args: tuple,
) -> None:
    # `generate` / `config` are their own click.Command/click.Group objects
    # (tommy/generate_cmd.py), dispatched here rather than making `main`
    # itself a click.Group — that would break `tommy [FLAGS] [-- harness
    # args]`, which relies on ignore_unknown_options + a single UNPROCESSED
    # extra_args catch-all. Recognized here as the first unprocessed
    # positional, exactly like everything else that falls into extra_args.
    if extra_args and extra_args[0] == "generate":
        generate_command.main(args=list(extra_args[1:]), prog_name="tommy generate", standalone_mode=True)
        return
    if extra_args and extra_args[0] == "config":
        config_group.main(args=list(extra_args[1:]), prog_name="tommy config", standalone_mode=True)
        return
    if "--help" in extra_args or "-h" in extra_args:
        click.echo(ctx.get_help())
        return

    cfg = TommyConfig.load(conf_path=Path(conf) if conf else None)

    # Logo + terminal title (only when actually launching, not for --install/--list-*)
    if not (do_upgrade or mcp_mode or do_install or list_projects or list_harnesses):
        _print_banner(cfg, project_key=project)

    # --upgrade
    if do_upgrade:
        _do_upgrade()
        return

    # --mcp: hand off to stdio MCP server immediately; editor owns our lifecycle
    if mcp_mode:
        run_stdio()
        return

    # --install
    if do_install:
        run_install(force=force)
        return

    # --list-projects
    if list_projects:
        if not cfg.projects:
            click.echo("No projects configured. Edit PROJECTS in tommy.conf.")
            return
        click.echo("Configured projects:")
        for p in cfg.projects:
            click.echo(f"  {p.key:<12} {p.name:<16} JIRA={p.jira_project}  {p.git_root}")
        return

    # --list-harnesses
    if list_harnesses:
        harnesses = all_harnesses()
        click.echo("Harnesses:")
        for name, spec in harnesses.items():
            status = "✓" if spec.available else "✗"
            active = " ← active" if name == cfg.harness else ""
            click.echo(f"  {status} {name:<16} {spec.description}{active}")
        return

    # ── memnos startup sequence ──────────────────────────────────────────
    client = None
    if not no_memnos_check:
        memnos_ok = _ensure_memnos_running(cfg)
        if memnos_ok:
            client = _memnos_client(cfg)
            if client:
                click.echo("🟢 memnos connected (HTTP)")
                _journal_start(client, cfg, project)
            else:
                click.echo(
                    f"⚠️  memnos reachable but SDK init failed — memory features disabled.",
                    err=True,
                )
        else:
            click.echo(
                f"⚠️  memnos not reachable at {cfg.memnos_url} — memory features disabled.\n"
                "   (Start memnos or use --no-memnos-check to suppress this warning.)",
                err=True,
            )
    # ────────────────────────────────────────────────────────────────────

    # ── auto-ingest design docs into the corpus (issue #109) ──────────────
    # Workspace root for both tommy.yaml discovery and the git-diff window:
    # the selected project's git_root if one is active, else CWD — mirrors
    # _launch_harness()'s own ws_path resolution below (project git_root >
    # CWD), computed separately here since effective-config resolution and
    # the harness launch are independent steps that both need it.
    workspace_root = Path.cwd()
    if project:
        proj = cfg.project_by_key(project)
        if proj:
            candidate = Path(proj.git_root).expanduser().resolve()
            if candidate.is_dir():
                workspace_root = candidate
    try:
        effective = resolve_effective_config(project_root=workspace_root)
    except TommyYamlError as exc:
        # A broken tommy.yaml must not block the launch (fail-open) — this
        # is the interactive path; a developer needs their harness to start
        # even with a bad tommy.yaml. Surface it rather than silently
        # skipping auto_ingest, though: it may have been requested.
        click.echo(f"⚠️  tommy.yaml could not be read — auto_ingest skipped: {exc}", err=True)
        effective = None
    if effective is not None and effective.value("auto_ingest"):
        _auto_ingest_changed_docs(cfg, effective, client, workspace_root)
    # ────────────────────────────────────────────────────────────────────

    if ask_permissions:
        cfg.skip_permissions = False
    _launch_harness(cfg, project_key=project, extra_args=extra_args, memnos_client=client)
