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
from .control import ControlServer
from .discovery.harnesses import all_harnesses, apply_skip_permissions, apply_session_name


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
    _session_name = f"Tommy | {project_key.upper()}" if project_key else "Tommy"
    cmd = apply_session_name(cmd, cfg.harness, _session_name)

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
            click.echo(f"   Workspace: {proj.git_root}")

    # Subscribe to namespace BEFORE launching (capture what sub-agent writes)
    sub_id: Optional[int] = None
    if memnos_client:
        try:
            sub = memnos_client.namespace_subscribe()
            sub_id = sub.get("subscription_id")
        except Exception:
            pass

    run_id = str(uuid.uuid4())[:8]

    # ── Control channel (bidirectional IPC) ──────────────────────────────────
    ctrl = ControlServer(
        on_message=_on_ctrl_message,
        connect_timeout=30.0,
    )
    env["TOMMY_CTRL_PORT"] = str(ctrl.port)
    # ─────────────────────────────────────────────────────────────────────────────

    # ── Popen: Tommy stays alive ──────────────────────────────────────────
    # Resolve working directory: project git_root > CWD
    ws_path: Path = Path.cwd()
    if project_key:
        proj = cfg.project_by_key(project_key)
        if proj:
            candidate = Path(proj.git_root).expanduser().resolve()
            if candidate.is_dir():
                ws_path = candidate

    proc = subprocess.Popen(cmd, env=env, cwd=str(ws_path))
    proc.wait()
    exit_code = proc.returncode
    ctrl.close()
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
    click.echo(f"  {bold}memnos-native coding orchestrator{r}  {g}v0.1.0{r}", err=True)



    click.echo("", err=True)



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
@click.option("--ask-permissions", is_flag=True, help="Require manual approval for every harness tool call (overrides SKIP_PERMISSIONS=on).")
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
    ask_permissions: bool,
    extra_args: tuple,
) -> None:
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

    if ask_permissions:
        cfg.skip_permissions = False
    _launch_harness(cfg, project_key=project, extra_args=extra_args, memnos_client=client)
