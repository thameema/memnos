"""
`tommy generate` and `tommy config show` — the tommy.yaml-facing CLI surface.

Deliberately its own module (not folded into cli.py's giant `main`) and
deliberately NOT added to memnos's own `memnos_cli.py` — see issue #113:
Tommy's CLI is released independently of memnos/memnos-sdk (RELEASING.md),
and this command only makes sense in Tommy's world.

cli.py's `main()` stays a single click.Command (not a click.Group) for
backward compatibility with `tommy [FLAGS] [-- harness args]` — see the
dispatch comment in cli.py. These two commands are built as their own
click.Command/click.Group objects and invoked directly from `main()` when
the first unprocessed positional arg is "generate" or "config", using
click's own `.main(args=..., standalone_mode=True)` so `--help`, error
messages, and exit codes all behave like a normal subcommand would.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import click

from .adapters import generate_adapters, render_stdout_fallback
from .effective_config import EffectiveConfig, resolve_effective_config
from .project_config import TommyYamlError


def _resolve_or_die(
    conf: Optional[str], tommy_yaml: Optional[str], root: Optional[str] = None,
) -> EffectiveConfig:
    try:
        return resolve_effective_config(
            conf_path=Path(conf) if conf else None,
            tommy_yaml_path=Path(tommy_yaml) if tommy_yaml else None,
            project_root=Path(root) if root else None,
        )
    except TommyYamlError as exc:
        click.echo(f"✗ {exc}", err=True)
        sys.exit(1)


# ---------------------------------------------------------------------------
# tommy generate
# ---------------------------------------------------------------------------


@click.command(
    "generate",
    help="Read tommy.yaml and write/update harness adapter files "
    "(CLAUDE.md, .cursor/rules/tommy.mdc, .windsurfrules, .github/copilot-instructions.md).",
)
@click.option("--conf", default=None, metavar="PATH", help="Path to tommy.conf override.")
@click.option("--tommy-yaml", default=None, metavar="PATH", help="Explicit path to tommy.yaml (skips discovery).")
@click.option("--root", "root_opt", default=None, metavar="PATH",
              help="Project root to write adapter files under (default: tommy.yaml's directory, or CWD).")
@click.option("--create-missing", is_flag=True,
              help="Also create adapter files that show no prior evidence of that harness being used.")
@click.option("--dry-run", is_flag=True, help="Show what would be written without writing anything.")
def generate_command(
    conf: Optional[str],
    tommy_yaml: Optional[str],
    root_opt: Optional[str],
    create_missing: bool,
    dry_run: bool,
) -> None:
    effective = _resolve_or_die(conf, tommy_yaml, root_opt)

    if root_opt:
        project_root = Path(root_opt).resolve()
    elif effective.tommy_yaml_path:
        project_root = effective.tommy_yaml_path.parent
    else:
        project_root = Path.cwd()

    results = generate_adapters(
        effective, project_root, create_missing=create_missing, dry_run=dry_run,
    )

    any_touched = any(r.action != "skipped" for r in results)
    if not any_touched:
        click.echo(render_stdout_fallback(effective))
        return

    for r in results:
        if r.action == "skipped":
            click.echo(f"  · {r.target.name:<8} {r.path}  (no evidence of use — skipped; --create-missing to force)")
        elif r.action.startswith("dry-run:"):
            inner = r.action.split(":", 1)[1]
            click.echo(f"  ○ {r.target.name:<8} {r.path}  (dry-run: would be {inner})")
        elif r.action == "unchanged":
            click.echo(f"  ✓ {r.target.name:<8} {r.path}  (already up to date)")
        else:
            click.echo(f"  ✓ {r.target.name:<8} {r.path}  ({r.action})")


# ---------------------------------------------------------------------------
# tommy config show
# ---------------------------------------------------------------------------


_FIELD_ORDER = [
    "project_name", "project_key", "project_git_root",
    "namespace", "design_docs",
    "corpus_gate", "auto_ingest",
    "default_model", "harness", "smart_routing", "mcp_introspect", "skip_permissions",
    "merge_gate", "wave_limit",
]


def _render_text(effective: EffectiveConfig) -> str:
    lines = []
    yaml_loc = str(effective.tommy_yaml_path) if effective.tommy_yaml_path else "(none found)"
    lines.append(f"tommy.yaml: {yaml_loc}")
    lines.append("")
    lines.append("Effective config (precedence: tommy.conf -> tommy.yaml -> env):")
    width = max(len(n) for n in _FIELD_ORDER)
    for name in _FIELD_ORDER:
        rf = effective.fields[name]
        lines.append(f"  {name:<{width}}  {rf.value!r:<30} [{rf.source}]")
    return "\n".join(lines)


def _render_json(effective: EffectiveConfig) -> str:
    payload = {
        "tommy_yaml_path": str(effective.tommy_yaml_path) if effective.tommy_yaml_path else None,
        "fields": effective.as_provenance_dict(),
    }
    return json.dumps(payload, indent=2, sort_keys=True, default=str)


@click.group("config", help="Inspect Tommy's resolved configuration.")
def config_group() -> None:
    pass


@config_group.command("show", help="Print the fully-resolved effective config (after tommy.conf -> tommy.yaml -> env precedence).")
@click.option("--conf", default=None, metavar="PATH", help="Path to tommy.conf override.")
@click.option("--tommy-yaml", default=None, metavar="PATH", help="Explicit path to tommy.yaml (skips discovery).")
@click.option("--root", default=None, metavar="PATH", help="Directory to start tommy.yaml discovery from (default: CWD).")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text", help="Output format.")
def config_show(conf: Optional[str], tommy_yaml: Optional[str], root: Optional[str], fmt: str) -> None:
    effective = _resolve_or_die(conf, tommy_yaml, root)
    if fmt == "json":
        click.echo(_render_json(effective))
    else:
        click.echo(_render_text(effective))
