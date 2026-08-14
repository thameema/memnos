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
"""
from __future__ import annotations

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

from .config import TommyConfig
from .prompt import build_prompt
from .install import run_install
from .mcp_server import run_stdio
from .discovery.harnesses import all_harnesses


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
            urllib.request.urlopen(f"{cfg.memnos_url}/health", timeout=2)
            return True
        except Exception:
            return False

    if _reachable():
        return True

    # Not reachable — try to start the daemon
    click.echo("  memnos not running — attempting auto-start...", err=True)
    try:
        subprocess.Popen(
            ["memnos", "start", "--http", "--port", "8900"],
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
      ~/.claude/projects/<hash>/conversations/<uuid>.jsonl
    """
    pattern = str(Path.home() / ".claude" / "projects" / "*" / "conversations" / "*.jsonl")
    files = glob.glob(pattern)
    if not files:
        return None
    return Path(max(files, key=os.path.getmtime))


def _post_run_capture(client, cfg: TommyConfig, run_id: str, project_key: Optional[str]) -> None:
    """
    After the sub-agent exits:
    1. Poll namespace_feed for anything the sub-agent wrote to memnos
    2. Ingest the Claude Code conversation transcript
    3. Call segment_episodes so this run becomes a searchable episode
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

        # Layer 4: segment episodes
        try:
            client.segment_episodes(gap_minutes=30)
            click.echo("  🧠 Episode segmented → memnos")
        except Exception:
            pass  # segment_episodes is best-effort

        # Journal completion
        proj_note = f", project={project_key}" if project_key else ""
        client.remember(
            f"Tommy session ended (run_id={run_id}{proj_note}, transcript_ingested={transcript is not None})",
            namespace=cfg.tommy_ns,
        )
    except Exception:
        pass  # capture is best-effort — never block the user


# ---------------------------------------------------------------------------
# Launch harness via Popen (stays alive for capture)
# ---------------------------------------------------------------------------

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

    # Inject MEMNOS_URL so the sub-agent's MCP config picks it up
    env = os.environ.copy()
    env["MEMNOS_URL"] = cfg.memnos_url
    env["TOMMY_NS"] = cfg.tommy_ns
    env["TOMMY_DEFAULT_NS"] = cfg.default_ns

    click.echo(f"🟣 Tommy → {cfg.harness}  (smart_routing={'on' if cfg.smart_routing else 'off'})")
    if project_key:
        proj = cfg.project_by_key(project_key)
        if proj:
            click.echo(f"   Project: {proj.name} ({proj.jira_project}) @ {proj.git_root}")

    # Subscribe to namespace BEFORE launching (capture what sub-agent writes)
    sub_id: Optional[int] = None
    if memnos_client:
        try:
            sub = memnos_client.namespace_subscribe()
            sub_id = sub.get("subscription_id")
        except Exception:
            pass

    run_id = str(uuid.uuid4())[:8]

    # ── Popen: Tommy stays alive ──────────────────────────────────────────
    proc = subprocess.Popen(cmd, env=env)
    proc.wait()
    exit_code = proc.returncode
    # ─────────────────────────────────────────────────────────────────────

    # Cleanup temp file
    try:
        os.unlink(prompt_file)
    except OSError:
        pass

    # Post-run capture (Layer 2: feed, Layer 3: ingest, Layer 4: segment)
    if memnos_client:
        # Layer 2: drain namespace feed
        if sub_id is not None:
            try:
                new_memories = memnos_client.namespace_feed(sub_id)
                count = len(new_memories.get("items", []))
                if count:
                    click.echo(f"  📡 {count} new memor{'y' if count == 1 else 'ies'} written by sub-agent")
            except Exception:
                pass

        _post_run_capture(memnos_client, cfg, run_id, project_key)

    sys.exit(exit_code)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _do_upgrade() -> None:
    """Upgrade Tommy using the same installer that installed it (uv / pipx / pip)."""
    import shutil
    import subprocess
    from pathlib import Path

    home = Path.home()
    exe  = Path(sys.executable).resolve()
    uv_tools_root = (home / ".local" / "share" / "uv" / "tools").resolve()

    # 1. uv tool path heuristic (fastest, works even with broken CWD)
    if str(exe).startswith(str(uv_tools_root)) and shutil.which("uv"):
        cmd = ["uv", "tool", "upgrade", "tommy-orchestrator"]
    # 2. uv tool list (safe CWD)
    elif shutil.which("uv"):
        try:
            out = subprocess.run(
                ["uv", "tool", "list"], capture_output=True, text=True, cwd=str(home),
            ).stdout
            if "tommy" in out:
                cmd = ["uv", "tool", "upgrade", "tommy-orchestrator"]
            else:
                cmd = [sys.executable, "-m", "pip", "install", "-U", "tommy-orchestrator"]
        except Exception:
            cmd = [sys.executable, "-m", "pip", "install", "-U", "tommy-orchestrator"]
    # 3. pipx
    elif shutil.which("pipx"):
        try:
            out = subprocess.run(
                ["pipx", "list", "--short"], capture_output=True, text=True, cwd=str(home),
            ).stdout
            if "tommy" in out:
                cmd = ["pipx", "upgrade", "tommy-orchestrator"]
            else:
                cmd = [sys.executable, "-m", "pip", "install", "-U", "tommy-orchestrator"]
        except Exception:
            cmd = [sys.executable, "-m", "pip", "install", "-U", "tommy-orchestrator"]
    else:
        click.echo(
            "  [warn] falling back to pip — if this fails, run:\n"
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


@click.command(
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
    help="Tommy — personal coding orchestrator built on memnos.",
)
@click.option("--conf", default=None, metavar="PATH", help="Path to tommy.conf override.")
@click.option("--project", "-p", default=None, metavar="KEY", help="Activate a project context.")
@click.option("--install", "do_install", is_flag=True, help="Run first-time setup.")
@click.option("--force", is_flag=True, help="With --install: overwrite existing files.")
@click.option("--list-projects", is_flag=True, help="List configured projects.")
@click.option("--list-harnesses", is_flag=True, help="List detected harnesses.")
@click.option("--mcp", "mcp_mode", is_flag=True, help="Run as MCP stdio server (editor-managed subprocess, no daemon, no port).")
@click.option("--upgrade", "do_upgrade", is_flag=True, help="Upgrade Tommy via uv tool install (same venv, no pip).")
@click.option("--no-memnos-check", is_flag=True, help="Skip memnos health check at startup.")
@click.argument("extra_args", nargs=-1, type=click.UNPROCESSED)
def main(
    conf: Optional[str],
    project: Optional[str],
    do_install: bool,
    force: bool,
    list_projects: bool,
    list_harnesses: bool,
    mcp_mode: bool,
    do_upgrade: bool,
    no_memnos_check: bool,
    extra_args: tuple,
) -> None:
    cfg = TommyConfig.load(conf_path=Path(conf) if conf else None)

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

    _launch_harness(cfg, project_key=project, extra_args=extra_args, memnos_client=client)
