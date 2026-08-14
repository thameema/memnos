"""
Tommy CLI entrypoint.

Usage:
    tommy                           # launch with default harness
    tommy --project hdig            # activate project context
    tommy --install                 # first-time setup
    tommy --list-projects           # show configured projects
    tommy --list-harnesses          # show detected harnesses
    tommy --conf /path/to/conf      # explicit config file
    tommy --force                   # combined with --install: overwrite existing
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Optional

import click

from .config import TommyConfig
from .prompt import build_prompt
from .install import run_install
from .discovery.harnesses import all_harnesses


def _memnos_health(cfg: TommyConfig) -> bool:
    """Return True if memnos server is reachable."""
    try:
        from memnos_sdk import MemnosClient  # type: ignore
        client = MemnosClient(
            base_url=cfg.memnos_url,
            token=cfg.memnos_token,
            namespace=cfg.tommy_ns,
        )
        return client.healthy()
    except Exception:
        return False


def _journal_start(cfg: TommyConfig, project_key: Optional[str]) -> None:
    """Write a 'session started' memory to TOMMY_NS."""
    try:
        from memnos_sdk import MemnosClient  # type: ignore
        client = MemnosClient(
            base_url=cfg.memnos_url,
            token=cfg.memnos_token,
            namespace=cfg.tommy_ns,
        )
        proj_note = f", project={project_key}" if project_key else ""
        client.remember(
            f"Tommy session started (harness={cfg.harness}, model={cfg.default_model}"
            f", smart_routing={'on' if cfg.smart_routing else 'off'}{proj_note})",
            namespace=cfg.tommy_ns,
        )
    except Exception:
        pass  # memnos journaling is best-effort


def _launch_harness(cfg: TommyConfig, project_key: Optional[str], extra_args: tuple) -> None:
    """Build prompt, write to temp file, exec the configured harness."""
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

    # Write prompt to a temp file (harnesses read from file, not stdin)
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".md",
        prefix="tommy-prompt-",
        delete=False,
    ) as f:
        f.write(prompt)
        prompt_file = f.name

    cmd = [
        part.replace("{prompt_file}", prompt_file)
        for part in spec.launch_template
    ] + list(extra_args)

    click.echo(f"🟣 Tommy → {cfg.harness}  (smart_routing={'on' if cfg.smart_routing else 'off'})")
    if project_key:
        proj = cfg.project_by_key(project_key)
        if proj:
            click.echo(f"   Project: {proj.name} ({proj.jira_project}) @ {proj.git_root}")

    # exec — replace this process with the harness
    os.execvp(cmd[0], cmd)
    # never reached (exec replaces the process); temp file is cleaned by OS on exit


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
@click.option("--no-memnos-check", is_flag=True, help="Skip memnos health check at startup.")
@click.argument("extra_args", nargs=-1, type=click.UNPROCESSED)
def main(
    conf: Optional[str],
    project: Optional[str],
    do_install: bool,
    force: bool,
    list_projects: bool,
    list_harnesses: bool,
    no_memnos_check: bool,
    extra_args: tuple,
) -> None:
    cfg = TommyConfig.load(conf_path=Path(conf) if conf else None)

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

    # memnos health check
    if not no_memnos_check:
        if _memnos_health(cfg):
            click.echo("🟢 memnos connected")
            _journal_start(cfg, project)
        else:
            click.echo(
                f"⚠️  memnos not reachable at {cfg.memnos_url} — memory features disabled.\n"
                "   (Start memnos or use --no-memnos-check to suppress this warning.)",
                err=True,
            )

    _launch_harness(cfg, project_key=project, extra_args=extra_args)
