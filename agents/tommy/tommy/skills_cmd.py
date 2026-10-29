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
from datetime import datetime
from pathlib import Path
from typing import Optional

import click

from tommy.skill_match import Index

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


MIN_AGE_DAYS = 7   # a skill installed more recently than this has had no fair chance to fire yet


def _age_days(iso: Optional[str]) -> Optional[float]:
    try:
        return (time.time() - datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()) / 86400
    except (AttributeError, ValueError):
        return None


def installed_skills(claude_dir: Path) -> list[dict]:
    """One row per skill Claude will list every turn: {name, plugin, desc_chars, age_days}. Covers plugin
    skills and ~/.claude/skills, since both cost the same prompt overhead. age_days is None when unknown."""
    rows = []
    for key, entries in _installed(claude_dir).items():
        ages = [a for a in (_age_days(e.get("installedAt")) for e in entries) if a is not None]
        for e in entries:
            for md in Path(e.get("installPath", "")).glob("skills/*/SKILL.md"):
                fm = _frontmatter(md)
                rows.append({"name": fm.get("name") or md.parent.name, "plugin": key.split("@")[0],
                             "desc_chars": len(fm.get("description", "")), "desc": fm.get("description", ""),
                             "age_days": max(ages, default=None)})
    for md in (claude_dir / "skills").glob("*/SKILL.md"):
        fm = _frontmatter(md)
        st = md.parent.stat()
        born = getattr(st, "st_birthtime", st.st_mtime)   # no install record for a hand-copied skill
        rows.append({"name": fm.get("name") or md.parent.name, "plugin": "(user skills)",
                     "desc_chars": len(fm.get("description", "")), "desc": fm.get("description", ""),
                     "age_days": (time.time() - born) / 86400})
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
    click.echo(f"{'uses':>5}  {'skill':<38} {'from':<30} {'age':>5}  desc chars")
    for r in rows:
        age = "?" if r["age_days"] is None else f"{r['age_days']:.0f}d"
        click.echo(f"{r['uses']:>5}  {r['name']:<38} {r['plugin']:<30} {age:>5}  {r['desc_chars']}")
    unused = [r for r in rows if r["uses"] == 0]
    new = [r for r in unused if r["age_days"] is not None and r["age_days"] < MIN_AGE_DAYS]
    old = [r for r in unused if r not in new]
    click.echo(f"\n{len(rows)} installed. {len(old)} never used in this window and installed {MIN_AGE_DAYS}+ days ago "
               f"(or age unknown): ~{sum(r['desc_chars'] for r in old) // 4} tokens of description on every turn. "
               f"{len(new)} more were installed under {MIN_AGE_DAYS} days ago: too new to judge.")
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
        up = _src_url(src).removesuffix(".git").removeprefix("https://github.com/") if isinstance(src, dict) else "(this repo)"
        pin = (src.get("sha") or "")[:7] if isinstance(src, dict) else "-"
        click.echo(f"{'*' if e['name'] in have else ' ':2}{e['name']:<26} {up:<44} {pin:<8} {e.get('license', '?')}")
    click.echo("\n* = installed")


def _src_url(src: dict) -> str:
    """Clone URL of a marketplace source: git-subdir has `url`, a whole-repo github source has `repo`."""
    return src.get("url") or f"https://github.com/{src.get('repo', '')}.git"


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
        url = _src_url(src)
        latest = _latest_sha(url)
        if latest is None:
            click.echo(f"  ? {e['name']:<26} could not reach {url}")
        elif latest == src["sha"]:
            click.echo(f"  = {e['name']:<26} up to date ({latest[:7]})")
        else:
            base = url.removesuffix(".git")
            click.echo(f"  ^ {e['name']:<26} {src['sha'][:7]} -> {latest[:7]}   review: {base}/compare/{src['sha'][:12]}...{latest[:12]}")
            if e["name"] in apply_names:
                src["sha"], changed = latest, True
                click.echo(f"      pinned {e['name']} to {latest[:7]}")
    if changed:
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


# --- productivity: what happened after each skill fired -------------------------------------------------
# These are proxies, not a quality score: a skill can be used and useless, or quiet and essential.
_CORRECTION = re.compile(r"\b(wrong|incorrect|undo|revert|that'?s not|not what i|why did you)\b|^\s*no[,.!]", re.I)
_ACTION_TOOLS = {"Edit", "Write", "NotebookEdit", "Bash"}


def _human_text(content) -> Optional[str]:
    """Text of a real human prompt, or None for tool results / non-prompt events."""
    if isinstance(content, str):
        return content
    if isinstance(content, list) and not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
        return " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return None


def outcomes(projects_dir: Path, since_days: Optional[int] = None) -> tuple[dict, int]:
    """Per skill name: uses, sessions, and what followed each use up to the next human prompt:
    actions (Edit/Write/Bash calls), tool errors, and whether that next prompt reads as a correction.
    Sub-agent transcripts are skipped: they have no human prompts to react to."""
    cutoff = time.time() - since_days * 86400 if since_days else 0
    stats: dict = {}
    n = 0
    for f in projects_dir.rglob("*.jsonl"):
        if "subagents" in f.parts or f.stat().st_mtime < cutoff:
            continue
        n += 1
        open_w: list = []   # stat dicts still waiting for the next human prompt
        seen: set = set()
        with open(f, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                msg = o.get("message") or {}
                content = msg.get("content")
                blocks = content if isinstance(content, list) else []
                if o.get("type") == "user" or msg.get("role") == "user":
                    # injected content (a skill's body arrives as an isMeta user message) is not the human reacting
                    text = None if (o.get("isMeta") or o.get("sourceToolUseID")) else _human_text(content)
                    if text is not None and text.strip() and not text.lstrip().startswith("<system-reminder>"):
                        for w in open_w:
                            w["corrections"] += bool(_CORRECTION.search(text))
                        open_w = []
                    for b in blocks:
                        if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error"):
                            for w in open_w:
                                w["errors"] += 1
                    continue
                for b in blocks:
                    if not (isinstance(b, dict) and b.get("type") == "tool_use"):
                        continue
                    if b.get("name") == "Skill" and (b.get("input") or {}).get("skill"):
                        name = b["input"]["skill"]
                        st = stats.setdefault(name, {"uses": 0, "sessions": 0, "actions": 0, "errors": 0, "corrections": 0})
                        st["uses"] += 1
                        if name not in seen:
                            seen.add(name)
                            st["sessions"] += 1
                        open_w.append(st)
                    elif b.get("name") in _ACTION_TOOLS:
                        for w in open_w:
                            w["actions"] += 1
    return stats, n


def _verdict(uses: int, errors: int, corrections: int, age_days: Optional[float] = None) -> str:
    if uses == 0 and age_days is not None and age_days < MIN_AGE_DAYS:
        return f"too new to judge (installed {age_days:.0f}d ago)"
    if uses == 0:
        return "idle: look at why (description unclear? not relevant yet? shadowed by another skill?)"
    if uses < 3:
        return "too few uses to judge"
    if corrections / uses >= 0.5 or errors / uses >= 3:
        return "review: often followed by corrections or errors"
    return "keep"


@skills_group.command("eval")
@click.option("--days", default=30, show_default=True, help="Only transcripts modified in the last N days (0 = all).")
def eval_cmd(days: int) -> None:
    """Productivity scorecard per skill from your own transcripts: how often it fires, in how many sessions,
    what followed (edits/commands, tool errors, a correction from you), and the prompt cost of keeping it.
    Proxies only: read the verdict as a prompt to look, not as a grade."""
    cd = _claude_dir()
    stats, n = outcomes(cd / "projects", days or None)
    merged = {}
    for r in installed_skills(cd):
        agg = {"uses": 0, "sessions": 0, "actions": 0, "errors": 0, "corrections": 0}
        for k in (r["name"], f"{r['plugin']}:{r['name']}"):
            for f in agg:
                agg[f] += stats.get(k, {}).get(f, 0)
        merged[(r["plugin"], r["name"])] = {**agg, "desc_chars": r["desc_chars"], "age_days": r["age_days"]}
    click.echo(f"{n} session transcript(s) scanned" + (f" (last {days} days)" if days else "") + "\n")
    click.echo(f"{'skill':<34} {'from':<22} {'uses':>4} {'sess':>4} {'acts/use':>8} {'err/use':>7} {'corr':>4} {'tok/turn':>8}  verdict")
    for (plugin, name), m in sorted(merged.items(), key=lambda kv: (-kv[1]["uses"], kv[0])):
        u = m["uses"] or 1
        click.echo(f"{name:<34} {plugin:<22} {m['uses']:>4} {m['sessions']:>4} {m['actions'] / u:>8.1f} "
                   f"{m['errors'] / u:>7.1f} {m['corrections']:>4} {m['desc_chars'] // 4:>8}  "
                   f"{_verdict(m['uses'], m['errors'], m['corrections'], m['age_days'])}")
    click.echo("\nacts/use = edits/commands that followed a use; err/use = tool errors after it; corr = times your next prompt "
               "looked like a correction. tok/turn = description cost paid every turn whether or not it fires.")


# --- vetting: read an upstream repo before it is pinned -------------------------------------------------
def _clone(url: str, dest: Path) -> Optional[str]:
    r = subprocess.run(["git", "clone", "-q", "--depth", "1", url, str(dest)], capture_output=True, text=True, timeout=180)
    if r.returncode:
        return None
    return subprocess.run(["git", "-C", str(dest), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()


@skills_group.command("vet")
@click.argument("repo")
def vet_cmd(repo: str) -> None:
    """Read-only look at an upstream repo (owner/name or URL) before adding it: licence, skills, agents,
    hooks, scripts and code-execution or network patterns, then the pinned marketplace entry to review.
    Adds nothing."""
    import tempfile
    url = repo if repo.startswith("http") else f"https://github.com/{repo}.git"
    with tempfile.TemporaryDirectory() as td:
        dest = Path(td) / "r"
        sha = _clone(url, dest)
        if not sha:
            raise click.ClickException(f"could not clone {url}")
        lic = next((p.name for p in dest.glob("LICENSE*")), None)
        lic_txt = (dest / lic).read_text("utf-8", "ignore")[:600] if lic else ""
        skills = [p for p in dest.rglob("SKILL.md")]
        agents = [p for p in dest.glob("**/agents/*.md")]
        hooks = sorted(str(p.relative_to(dest)) for p in dest.rglob("hooks.json"))
        scripts = sorted(str(p.relative_to(dest)) for p in dest.rglob("*")
                         if p.is_file() and p.suffix in (".sh", ".py", ".js", ".ts") and ".git" not in p.parts
                         and "node_modules" not in p.parts)
        pat = re.compile(r"curl |wget |requests\.|urllib|fetch\(|eval\(|exec\(|subprocess|child_process|os\.system|rm -rf")
        risky = []
        for p in scripts[:200]:
            hits = {m.group(0).strip() for m in pat.finditer((dest / p).read_text("utf-8", "ignore"))}
            if hits:
                risky.append(f"{p}: {', '.join(sorted(hits))}")
        slug = url.rstrip("/").removesuffix(".git").removeprefix("https://github.com/")
        click.echo(f"{url} @ {sha[:12]}")
        click.echo(f"licence file: {lic or 'NONE'}" + (f"  ({lic_txt.strip().splitlines()[0].strip()})" if lic_txt.strip() else ""))
        click.echo(f"skills: {len(skills)}   agents: {len(agents)}   hook configs: {len(hooks)}   scripts: {len(scripts)}")
        for h in hooks:
            click.echo(f"  HOOKS     {h}  <- read it: hooks run on every matching event")
        for r in risky[:15]:
            click.echo(f"  EXEC/NET  {r}")
        click.echo("\nRead the SKILL.md files and every hook or script above yourself. If it is fine, add this entry to "
                   ".claude-plugin/marketplace.json and a bundle plugin that depends on it:\n")
        click.echo(json.dumps({"name": slug.rsplit("/", 1)[-1],
                               "source": {"source": "github", "repo": slug, "sha": sha},
                               "license": "<fill in from the licence file>", "tags": ["tommy-curated"]}, indent=2))


# --- install: one command for a bundle, or everything --------------------------------------------------
BUNDLES = {
    "all": "everything below in one install (memory + all bundles, no pentesting)",
    "core": "memnos memory only",
    "engineering": "spec, plan, build, TDD, review and ship skills + sub-agents (agent-skills)",
    "architect": "C4 documentation and multi-perspective design review",
    "review": "differential security review, context building, variant analysis",
    "security": "sharp-edges, supply-chain, agentic-actions and static-analysis audits",
    "design-review": "HLD and LLD reviewers",
    "qa": "frontend, backend and REST API QA",
    "explain": "plain-English explanations with a real-world scenario",
}


def _plugin(bundle: str) -> str:
    return {"all": "tommy-all", "core": "tommy"}.get(bundle, f"tommy-{bundle}")


@skills_group.command("install")
@click.argument("bundle", required=False, type=click.Choice(sorted(BUNDLES)))
def install_cmd(bundle: Optional[str]) -> None:
    """Install a Tommy bundle (or `all`) through Claude Code, adding the memnos marketplace first if needed.
    Run with no argument to list the bundles."""
    if not bundle:
        for k, v in BUNDLES.items():
            click.echo(f"  {k:<14} {v}")
        click.echo("\nUsage: tommy skills install all")
        return
    import shutil
    claude = shutil.which("claude")
    if not claude:
        raise click.ClickException("the `claude` CLI is not on PATH. Install Claude Code first.")
    have = subprocess.run([claude, "plugin", "marketplace", "list"], capture_output=True, text=True).stdout
    if MARKETPLACE not in have:
        click.echo("adding the memnos marketplace ...")
        if subprocess.run([claude, "plugin", "marketplace", "add", "thameema/memnos"]).returncode:
            raise click.ClickException("could not add the marketplace: claude plugin marketplace add thameema/memnos")
    name = _plugin(bundle)
    click.echo(f"installing {name} ...")
    rc = subprocess.run([claude, "plugin", "install", f"{name}@{MARKETPLACE}"]).returncode
    if rc:
        raise click.ClickException(f"claude plugin install {name}@{MARKETPLACE} failed (exit {rc})")
    click.echo("done. Restart Claude Code sessions to load the new skills. `tommy skills eval` shows which ones you actually use.")


def _load_prompts(path: str) -> list[tuple[str, set]]:
    """JSONL rows {"prompt": str, "skill": name | [names] | null}. null = no skill should be hinted."""
    out = []
    for line in Path(path).read_text("utf-8").splitlines():
        if line.strip():
            o = json.loads(line)
            want = o.get("skill")
            want = set() if want is None else {want} if isinstance(want, str) else set(want)
            out.append((o["prompt"], {w.split(":")[-1] for w in want}))
    return out


@skills_group.command("bench")
@click.argument("prompts", type=click.Path(exists=True, dir_okay=False))
@click.option("-k", default=3, show_default=True, help="A hint names at most this many skills.")
def bench_cmd(prompts: str, k: int) -> None:
    """Offline check of the per-prompt skill hint: does keyword (BM25) ranking over your INSTALLED skills put
    the right one in the top k, and how often does it hint when nothing should fire? No model calls, free.
    PROMPTS is JSONL: {"prompt": "...", "skill": "name" | ["a","b"] | null}. A miss here caps what any hint can do."""
    rows = {r["name"]: r["desc"] for r in installed_skills(_claude_dir())}
    idx = Index(list(rows.items()))
    cases = _load_prompts(prompts)
    pos = [(p, w) for p, w in cases if w]
    skipped = [(p, w) for p, w in pos if not (w & rows.keys())]
    pos = [(p, w & rows.keys()) for p, w in pos if w & rows.keys()]
    neg = [p for p, w in cases if not w]
    if not pos:
        raise click.ClickException("none of the labelled skills are installed: nothing to measure")
    ranked = {p: idx.rank(p, k) for p, _ in pos}
    negr = {p: idx.rank(p, k) for p in neg}
    top1 = sorted(r[0][1] for r in list(ranked.values()) + list(negr.values()) if r)
    cuts = sorted({0.0, *(top1[int(len(top1) * q)] for q in (0.25, 0.5, 0.75))}) if top1 else [0.0]
    click.echo(f"{len(rows)} installed skills; {len(pos)} positive prompts, {len(neg)} negative"
               + (f"; {len(skipped)} skipped (labelled skill not installed)" if skipped else "") + "\n")
    click.echo(f"{'min score':>9} {'recall@1':>9} {'recall@' + str(k):>9} {'hinted':>7} {'false hint':>11}")
    for c in cuts:
        def hit(p, want, n):
            return any(nm in want for nm, sc in ranked[p][:n] if sc >= c)
        r1 = sum(hit(p, w, 1) for p, w in pos) / len(pos)
        rk = sum(hit(p, w, k) for p, w in pos) / len(pos)
        hinted = sum(any(sc >= c for _, sc in ranked[p]) for p, _ in pos) / len(pos)
        fh = (sum(any(sc >= c for _, sc in negr[p]) for p in neg) / len(neg)) if neg else float("nan")
        click.echo(f"{c:>9.2f} {r1:>9.0%} {rk:>9.0%} {hinted:>7.0%} {fh:>11.0%}")
    miss = [(p, w) for p, w in pos if not any(n in w for n, _ in ranked[p])]
    if miss:
        click.echo(f"\nmissed at min score 0 ({len(miss)}):")
        for p, w in miss[:15]:
            got = ", ".join(n for n, _ in ranked[p]) or "(nothing)"
            click.echo(f"  want {'/'.join(sorted(w))}: {p[:70]!r} -> {got}")
    click.echo("\nship bar for a hint: recall@k >= 90% with false hint < 10% at the same min score. "
               "Labels are hand-written; a pass here does not show the model picks better, only that the hint is accurate.")
