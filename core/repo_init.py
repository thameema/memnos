"""Cold-start repository hydration (`memnos init` — issue #236).

Hydrates a repository into memnos in <5 seconds with zero LLM calls:
1. Instruction/rule files (`CLAUDE.md`, `AGENTS.md`, `.cursorrules`,
   `.github/copilot-instructions.md`) -> verbatim `memory_type='constraint'` turns
   with deterministic `constraint_subject` keys (per issue #29: never LLM-paraphrased,
   and idempotent across re-runs via subject supersession).
2. Recent git history (`git log --since=<N>d --no-merges`) -> conventional commits,
   trailers, and architectural/fix commits tagged with `speaker='git'` (weighted at
   `MEMNOS_GIT_SPEAKER_WEIGHT=0.85` at recall rank time so organic session memories
   outrank bulk-hydrated git history at equal similarity).
"""
from __future__ import annotations

import hashlib
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

RULE_FILES = (
    "CLAUDE.md",
    "AGENTS.md",
    ".cursorrules",
    ".github/copilot-instructions.md",
)

_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$")
_BULLET_RE = re.compile(r"^\s*[-*+]\s+(.+?)\s*$")
_NUMBERED_RE = re.compile(r"^\s*\d+[.)]\s+(.+?)\s*$")
_RULE_CUE_RE = re.compile(
    r"\b(must|never|always|do not|don't|required|important|mandatory|"
    r"forbidden|prohibited|avoid|prefer|ensure|convention|rule|constraint|"
    r"guardrail|policy|invariant)\b",
    re.I,
)
_RULE_SECTION_RE = re.compile(
    r"\b(rules?|constraints?|conventions?|guidelines?|policies|policy|"
    r"guardrails?|invariants?|never|always|must|security|testing|style|"
    r"architecture|workflow|instructions?)\b",
    re.I,
)

_CONVENTIONAL_RE = re.compile(
    r"^(feat|fix|refactor|perf|security|arch|revert|docs|test|build|ci)"
    r"(\([^)]+\))?!?\s*:\s*(.+)$",
    re.I,
)
_FIX_COMMIT_RE = re.compile(
    r"^(fix|hotfix|bugfix|revert)(\([^)]+\))?!?\s*:|"
    r"\b(fixes|closes|resolves)\s+#\d+|\b(root[- ]cause|bug|regression)\b",
    re.I,
)
_DECISION_COMMIT_RE = re.compile(
    r"^(arch|refactor|perf|security)(\([^)]+\))?!?\s*:|"
    r"!\s*:|\b(breaking[- ]change|decision|architecture|migrate[ds]?)\b",
    re.I,
)
_TRIVIAL_COMMIT_RE = re.compile(
    r"^(wip|typo|lint|format|formatting|merge\b|bump\b|chore\(release\))",
    re.I,
)
_SIGNAL_COMMIT_RE = re.compile(
    r"(#\d+|\bv?\d+\.\d+\.\d+\b|"
    r"\b(?:fixes|closes|refs|decision|breaking[- ]change|root[- ]cause|co-authored-by)\s*:)",
    re.I,
)


def _deterministic_rule_subject(rel_path: str, rule_text: str) -> str:
    """Collision-safe, deterministic constraint_subject for a repo rule.
    Re-running `memnos init` produces the exact same subject for identical text,
    superseding the previous constraint row without duplication (issue #153/#236)."""
    norm_path = rel_path.strip().lower().replace("\\", "/")
    norm_text = " ".join((rule_text or "").lower().split())
    words = re.findall(r"[a-z0-9]+", norm_text)[:5]
    slug = "-".join(words)[:40].strip("-") or "rule"
    digest = hashlib.sha1(f"{norm_path}\x00{norm_text}".encode("utf-8")).hexdigest()[:10]
    path_tag = re.sub(r"[^a-z0-9]+", "-", norm_path).strip("-")[:20]
    return f"init:{path_tag}:{slug}-{digest}"


def parse_repo_rules(repo_root: str | Path, *, max_rules: int = 50) -> list[dict]:
    """Extract verbatim governance/instruction rules from standard repo instruction files.

    Never invokes an LLM (issue #29: constraints are guardrails and must never be
    paraphrased). Returns dicts ready for `/remember`:
      `{source_file, section, text, memory_type: 'constraint', constraint_subject}`
    """
    root = Path(repo_root).resolve()
    rules: list[dict] = []
    seen_subjects: set[str] = set()

    for rel in RULE_FILES:
        fpath = root / rel
        if not fpath.is_file():
            continue
        try:
            raw = fpath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        in_code_block = False
        current_section = rel
        is_dedicated_rules_file = rel in (".cursorrules", ".github/copilot-instructions.md")

        for line in raw.splitlines():
            stripped = line.strip()
            if stripped.startswith("```"):
                in_code_block = not in_code_block
                continue
            if in_code_block or not stripped:
                continue

            m_head = _HEADING_RE.match(line)
            if m_head:
                current_section = m_head.group(2).strip()
                continue

            m_bullet = _BULLET_RE.match(line) or _NUMBERED_RE.match(line)
            candidate = m_bullet.group(1).strip() if m_bullet else None
            if not candidate:
                if (is_dedicated_rules_file or _RULE_SECTION_RE.search(current_section)) and _RULE_CUE_RE.search(stripped):
                    candidate = stripped
                else:
                    continue

            # Skip pure markdown links / TOC entries or overly short fragments
            if len(candidate) < 15 or len(candidate) > 600:
                continue
            if candidate.startswith("[") and candidate.endswith(")") and "](" in candidate and len(candidate.split()) < 6:
                continue

            in_rule_section = is_dedicated_rules_file or bool(_RULE_SECTION_RE.search(current_section))
            has_rule_cue = bool(_RULE_CUE_RE.search(candidate))
            if not (in_rule_section or has_rule_cue):
                continue

            prefix = f"[{rel} :: {current_section}] " if current_section != rel else f"[{rel}] "
            verbatim_text = f"{prefix}{candidate}"
            subj = _deterministic_rule_subject(rel, candidate)
            if subj in seen_subjects:
                continue
            seen_subjects.add(subj)
            rules.append({
                "source_file": rel,
                "section": current_section,
                "text": verbatim_text,
                "memory_type": "constraint",
                "constraint_subject": subj,
            })
            if len(rules) >= max_rules:
                return rules

    return rules


def parse_git_history(
    repo_root: str | Path,
    *,
    since_days: int = 90,
    max_commits: int = 100,
) -> list[dict]:
    """Parse recent git commits into structured `speaker='git'` memory records.

    Filters out trivial noise (WIP/typo/merge/lint) and keeps conventional commits,
    commits with trailers/issue refs, or commits with explanatory bodies.
    """
    root = Path(repo_root).resolve()
    if not (root / ".git").exists():
        # Support git worktrees where .git is a file or parent repo
        try:
            chk = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
                capture_output=True, text=True, timeout=5, check=False,
            )
            if chk.returncode != 0:
                return []
        except Exception:
            return []

    fmt = "%H%x1f%aI%x1f%an%x1f%s%x1f%b%x1e"
    cmd = [
        "git", "-C", str(root), "log",
        f"--since={max(1, int(since_days))} days ago",
        f"-n{max(1, int(max_commits) * 2)}",
        "--no-merges",
        f"--format={fmt}",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
    except Exception:
        return []
    if proc.returncode != 0 or not proc.stdout:
        return []

    records: list[dict] = []
    for raw_rec in proc.stdout.split("\x1e"):
        raw_rec = raw_rec.strip()
        if not raw_rec:
            continue
        parts = raw_rec.split("\x1f")
        if len(parts) < 4:
            continue
        full_sha = parts[0].strip()
        iso_date = parts[1].strip()
        author_name = parts[2].strip()
        subject = parts[3].strip()
        body = parts[4].strip() if len(parts) > 4 else ""

        if not subject or len(subject) < 10:
            continue
        if _TRIVIAL_COMMIT_RE.search(subject):
            continue

        is_conv = bool(_CONVENTIONAL_RE.match(subject))
        has_signal = bool(_SIGNAL_COMMIT_RE.search(subject) or _SIGNAL_COMMIT_RE.search(body))
        has_body = len(body.splitlines()) >= 1 and len(body) >= 25
        if not (is_conv or has_signal or has_body):
            continue

        try:
            obs_dt = datetime.fromisoformat(iso_date.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            obs_dt = datetime.now(timezone.utc)

        combined = f"{subject}\n{body}" if body else subject
        if _FIX_COMMIT_RE.search(combined):
            mtype = "fix"
        elif _DECISION_COMMIT_RE.search(combined):
            mtype = "decision"
        else:
            mtype = "fact"

        short_sha = full_sha[:7]
        date_str = obs_dt.date().isoformat()
        trimmed_body = body[:500].strip()
        text = f"[git {short_sha}] ({date_str}) {subject}"
        if trimmed_body:
            text = f"{text}\n{trimmed_body}"

        records.append({
            "sha": short_sha,
            "full_sha": full_sha,
            "observed_at": obs_dt.isoformat(),
            "author_name": author_name,
            "subject": subject,
            "body": trimmed_body,
            "memory_type": mtype,
            "speaker": "git",
            "text": text,
        })
        if len(records) >= max_commits:
            break

    return records


def init_repo(
    repo_root: str | Path,
    post_fn: Callable[[str, dict], dict] | None = None,
    *,
    namespace: str | None = None,
    since_days: int = 90,
    max_commits: int = 50,
    max_rules: int = 50,
    include_rules: bool = True,
    include_git: bool = True,
    extract: bool = False,
    dry_run: bool = False,
) -> dict:
    """Hydrate a repository's rule files and recent git history into memnos.

    `post_fn(path, payload)` is the caller's `/remember` client (e.g. `memnos_cli._post`).
    Defaults to `extract=False` so cold-start hydration completes in <5 seconds with zero
    LLM calls; rules are always stored as `memory_type='constraint'` (verbatim per #29)
    and git commits as `speaker='git'`.
    """
    root = Path(repo_root).resolve()
    rules = parse_repo_rules(root, max_rules=max_rules) if include_rules else []
    commits = parse_git_history(root, since_days=since_days, max_commits=max_commits) if include_git else []

    rules_written = 0
    commits_written = 0
    retired_total = 0

    if not dry_run and post_fn is not None:
        for r in rules:
            body = {
                "text": r["text"],
                "speaker": "user",
                "type": "constraint",
                "constraint_subject": r["constraint_subject"],
                "extract": False,
            }
            if namespace:
                body["namespace"] = namespace
            res = post_fn("/remember", body) or {}
            if res.get("turn_id") or res.get("queued"):
                rules_written += 1
            retired_total += len(res.get("constraints_retired") or [])

        # Ingest commits oldest-first so supersession/chronology flows forward
        for c in reversed(commits):
            body = {
                "text": c["text"],
                "speaker": "git",
                "type": c["memory_type"],
                "session_id": f"git-init:{c['sha']}",
                "extract": bool(extract),
            }
            if namespace:
                body["namespace"] = namespace
            res = post_fn("/remember", body) or {}
            if res.get("turn_id") or res.get("queued"):
                commits_written += 1

    return {
        "repo_root": str(root),
        "namespace": namespace,
        "dry_run": dry_run,
        "rules_found": len(rules),
        "rules_written": rules_written,
        "constraints_retired": retired_total,
        "commits_found": len(commits),
        "commits_written": commits_written,
        "rule_sources": sorted({r["source_file"] for r in rules}),
        "rules": rules,
        "commits": commits,
    }
