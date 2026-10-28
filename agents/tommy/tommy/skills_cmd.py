"""
`tommy skills` — what curated skills are installed, whether they ever fire, and (for
maintainers) moving upstream pins.

Tommy ships no copies of other people's skills. The memnos marketplace lists curated
upstream plugins as `git-subdir` sources pinned to a commit, and the `tommy` plugin pulls
them in through plugin `dependencies`; this module only reads what Claude Code installed
and the transcripts it wrote.

Dispatched from cli.py like `generate` / `config` (main() must stay a single click.Command).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections import Counter
from pathlib import Path
from typing import Optional

import click

MARKETPLACE = "memnos"


def _claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def _installed(claude_dir: Path) -> dict:
    try:
        return json.loads((claude_dir / "plugins" / "installed_plugins.json").read_text("utf-8")).get("plugins", {})
    except (OSError, ValueError):
        return {}


def _marketplace_file(claude_dir: Path) -> Path:
    """Where Claude keeps the memnos marketplace: `installLocation` from known_marketplaces.json
    (a git clone for a GitHub add, the source folder itself for a local-path add)."""
    try:
        loc = json.loads((claude_dir / "plugins" / "known_marketplaces.json").read_text("utf-8"))[MARKETPLACE]["installLocation"]
    except (OSError, ValueError, KeyError):
        loc = str(claude_dir / "plugins" / "marketplaces" / MARKETPLACE)
    return Path(loc) / ".claude-plugin" / "marketplace.json"


def _frontmatter(path: Path) -> dict:
    """Top-level `key: value` pairs of the YAML frontmatter, including folded/literal (`>`/`|`) blocks."""
    m = re.match(r"---\n(.*?)\n---", path.read_text("utf-8", "replace"), re.S)
    out: dict = {}
    key = None
    for line in (m.group(1) if m else "").splitlines():
        if line[:1] in (" ", "\t") and key:
            out[key] = (out[key] + " " + line.strip()).strip()
        elif ":" in line:
            key, _, v = line.partition(":")
            key = key.strip()
            out[key] = "" if v.strip() in (">", "|", ">-", "|-") else v.strip()
    return out


def installed_skills(claude_dir: Path) -> list[dict]:
    """One row per skill Claude will list every turn: {name, plugin, desc_chars}. Covers plugin skills
    and ~/.claude/skills, since both cost the same prompt overhead."""
    rows = []
    for key, entries in _installed(claude_dir).items():
        for e in entries:
            for md in Path(e.get("installPath", "")).glob("skills/*/SKILL.md"):
                fm = _frontmatter(md)
                rows.append({"name": fm.get("name") or md.parent.name, "plugin": key.split("@")[0],
                             "desc_chars": len(fm.get("description", ""))})
    for md in (claude_dir / "skills").glob("*/SKILL.md"):
        fm = _frontmatter(md)
        rows.append({"name": fm.get("name") or md.parent.name, "plugin": "(user skills)",
                     "desc_chars": len(fm.get("description", ""))})
    return rows


def usage(projects_dir: Path, since_days: Optional[int] = None) -> tuple[Counter, Counter, int]:
    """Count Skill and sub-agent invocations in session transcripts. Skill calls are counted
    under the name the model used (`plugin:skill` or bare `skill`). Returns
    (skills, subagents, transcripts_scanned); files older than `since_days` are skipped."""
    cutoff = time.time() - since_days * 86400 if since_days else 0
    skills: Counter = Counter()
    agents: Counter = Counter()
    n = 0
    for f in projects_dir.rglob("*.jsonl"):   # includes <session>/subagents/*.jsonl, where dispatched reviewers fire skills
        if f.stat().st_mtime < cutoff:
            continue
        n += 1
        with open(f, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if '"tool_use"' not in line:
                    continue
                try:
                    content = json.loads(line).get("message", {}).get("content")
                except ValueError:
                    continue
                for b in content if isinstance(content, list) else []:
                    if not (isinstance(b, dict) and b.get("type") == "tool_use"):
                        continue
                    inp = b.get("input") or {}
                    if b.get("name") == "Skill" and inp.get("skill"):
                        skills[inp["skill"]] += 1
                    elif b.get("name") in ("Task", "Agent") and inp.get("subagent_type"):
                        agents[inp["subagent_type"]] += 1
    return skills, agents, n


def _uses(skills: Counter, name: str, plugin: str) -> int:
    """A skill may be called `name`, `plugin:name`, or (for a single-skill plugin) `plugin:plugin`."""
    return skills.get(name, 0) + skills.get(f"{plugin}:{name}", 0)


@click.group(name="skills", help="Inspect curated skills: what is installed, what actually fires, upstream pins.")
def skills_group() -> None:
    pass


@skills_group.command("stats")
@click.option("--days", default=30, show_default=True, help="Only transcripts modified in the last N days (0 = all).")
def stats_cmd(days: int) -> None:
    """Per-skill usage from your Claude Code transcripts, plus the prompt overhead of unused skills."""
    cd = _claude_dir()
    skills, agents, n = usage(cd / "projects", days or None)
    rows = installed_skills(cd)
    for r in rows:
        r["uses"] = _uses(skills, r["name"], r["plugin"])
    rows.sort(key=lambda r: (-r["uses"], r["plugin"], r["name"]))
    click.echo(f"{n} transcript(s) scanned" + (f" (last {days} days)" if days else "") + "\n")
    click.echo(f"{'uses':>5}  {'skill':<38} {'from':<30} desc chars")
    for r in rows:
        click.echo(f"{r['uses']:>5}  {r['name']:<38} {r['plugin']:<30} {r['desc_chars']}")
    unused = [r for r in rows if r["uses"] == 0]
    click.echo(f"\n{len(rows)} installed, {len(unused)} never used in this window. The unused ones still cost "
               f"~{sum(r['desc_chars'] for r in unused) // 4} tokens of description on every turn.")
    if agents:
        click.echo("\nsub-agents dispatched: " + ", ".join(f"{k} x{v}" for k, v in agents.most_common(10)))


@skills_group.command("list")
def list_cmd() -> None:
    """Curated upstream plugins in the memnos marketplace: where each comes from, its pin and licence."""
    cd = _claude_dir()
    try:
        entries = json.loads(_marketplace_file(cd).read_text("utf-8")).get("plugins", [])
    except (OSError, ValueError):
        raise click.ClickException(f"memnos marketplace not found under {cd}/plugins/marketplaces — run: "
                                   f"claude plugin marketplace add thameema/memnos")
    have = {k.split("@")[0] for k, v in _installed(cd).items() if k.endswith("@" + MARKETPLACE) and v}
    click.echo(f"{'':2}{'plugin':<26} {'upstream':<44} {'pin':<8} licence")
    for e in entries:
        src = e.get("source")
        up = src.get("url", "").removesuffix(".git").removeprefix("https://github.com/") if isinstance(src, dict) else "(this repo)"
        pin = (src.get("sha") or "")[:7] if isinstance(src, dict) else "-"
        click.echo(f"{'*' if e['name'] in have else ' ':2}{e['name']:<26} {up:<44} {pin:<8} {e.get('license', '?')}")
    click.echo("\n* = installed")


def _latest_sha(url: str) -> Optional[str]:
    r = subprocess.run(["git", "ls-remote", url, "HEAD"], capture_output=True, text=True, timeout=60)
    return r.stdout.split()[0] if r.returncode == 0 and r.stdout.strip() else None


@skills_group.command("update")
@click.option("--marketplace", "mp", type=click.Path(exists=True, dir_okay=False), default=None,
              help="marketplace.json to read/edit (default: .claude-plugin/marketplace.json in a repo checkout).")
@click.option("--apply", "apply_names", multiple=True, metavar="NAME", help="Move this plugin's pin to upstream HEAD.")
def update_cmd(mp: Optional[str], apply_names: tuple) -> None:
    """Maintainer tool: show where upstream has moved past each pin. Never changes a pin unless
    --apply NAME is given, and prints the compare URL so you can read the diff first."""
    path = Path(mp) if mp else Path(".claude-plugin/marketplace.json")
    if not path.exists():
        raise click.ClickException(f"{path} not found — run from a memnos checkout or pass --marketplace")
    data = json.loads(path.read_text("utf-8"))
    changed = False
    for e in data.get("plugins", []):
        src = e.get("source")
        if not (isinstance(src, dict) and src.get("sha")):
            continue
        latest = _latest_sha(src["url"])
        if latest is None:
            click.echo(f"  ? {e['name']:<26} could not reach {src['url']}")
        elif latest == src["sha"]:
            click.echo(f"  = {e['name']:<26} up to date ({latest[:7]})")
        else:
            base = src["url"].removesuffix(".git")
            click.echo(f"  ^ {e['name']:<26} {src['sha'][:7]} -> {latest[:7]}   review: {base}/compare/{src['sha'][:12]}...{latest[:12]}")
            if e["name"] in apply_names:
                src["sha"], changed = latest, True
                click.echo(f"      pinned {e['name']} to {latest[:7]}")
    if changed:
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
