#!/usr/bin/env python3
"""
migrate_obsidian.py — Migrate an Obsidian vault into the engram REST API.

Usage:
    python migrate_obsidian.py --vault /path/to/vault --namespace obsidian:my-vault --api-key <key>

Dependencies: requests, pyyaml
"""

import argparse
import re
import sys
import time
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Dependency check — fail fast with a helpful message
# ---------------------------------------------------------------------------

_missing = []
try:
    import requests
except ImportError:
    _missing.append("requests")

try:
    import yaml
except ImportError:
    _missing.append("pyyaml")

if _missing:
    print(
        f"[error] Missing required packages: {', '.join(_missing)}\n"
        f"        Run:  pip install requests pyyaml",
        file=sys.stderr,
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONTENT_LIMIT = 8000
INLINE_TAG_RE = re.compile(r"(?<!\w)#([A-Za-z][A-Za-z0-9_\-/]*)")
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+?)(?:\|[^\]]+)?\]\]")
FRONTMATTER_RE = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n?", re.DOTALL)
# Hex color codes (3 or 6 hex digits) mistakenly picked up as inline tags
_HEX_COLOR_RE = re.compile(r"^[0-9a-f]{3}(?:[0-9a-f]{3})?$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def parse_file(path: Path) -> dict:
    """
    Parse a single Obsidian markdown file.

    Returns a dict with keys:
        title, content, raw_content, tags, wikilinks, frontmatter
    """
    raw = path.read_text(encoding="utf-8", errors="replace")
    frontmatter: dict = {}
    body = raw

    m = FRONTMATTER_RE.match(raw)
    if m:
        try:
            parsed = yaml.safe_load(m.group(1))
            if isinstance(parsed, dict):
                frontmatter = parsed
        except yaml.YAMLError:
            pass
        body = raw[m.end():]

    # Title: frontmatter "title" field → filename stem
    title_raw = frontmatter.get("title") or frontmatter.get("Title")
    title = str(title_raw).strip() if title_raw else path.stem

    # Tags from frontmatter (various shapes: list, comma-string, single string)
    fm_tags: list[str] = []
    raw_tags = frontmatter.get("tags") or frontmatter.get("tag") or []
    if isinstance(raw_tags, list):
        fm_tags = [str(t).strip().lstrip("#") for t in raw_tags if t]
    elif isinstance(raw_tags, str):
        fm_tags = [t.strip().lstrip("#") for t in re.split(r"[,\s]+", raw_tags) if t.strip()]

    # Inline tags from body
    inline_tags = [t.lower() for t in INLINE_TAG_RE.findall(body)]

    # Combined, deduplicated, lowercase tags — strip hex color codes
    tags = list(dict.fromkeys(
        t for t in ([t.lower() for t in fm_tags] + inline_tags)
        if not _HEX_COLOR_RE.match(t)
    ))

    # Wikilinks
    wikilinks = list(dict.fromkeys(
        link.strip() for link in WIKILINK_RE.findall(body)
    ))

    # Content — strip frontmatter, truncate
    content = body.strip()
    if len(content) > CONTENT_LIMIT:
        content = content[:CONTENT_LIMIT] + "\n\n[truncated]"

    return {
        "title": title,
        "content": content,
        "raw_content": raw,
        "tags": tags,
        "wikilinks": wikilinks,
        "frontmatter": frontmatter,
    }


# ---------------------------------------------------------------------------
# Namespace helper
# ---------------------------------------------------------------------------


def note_namespace(vault_root: Path, note_path: Path, base_namespace: str) -> str:
    """
    Map folder structure to sub-namespace.

    vault/projects/backend/note.md + base obsidian:myvault
        → obsidian:myvault:projects:backend
    """
    rel = note_path.relative_to(vault_root)
    parts = rel.parts[:-1]  # exclude filename
    if parts:
        suffix = ":".join(p.replace(" ", "-").lower() for p in parts)
        return f"{base_namespace}:{suffix}"
    return base_namespace


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------


class EngramClient:
    def __init__(self, base_url: str, api_key: str, dry_run: bool = False):
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        self.dry_run = dry_run
        self._session = requests.Session()
        self._session.headers.update(self.headers)

    def write_memory(
        self,
        content: str,
        namespace: str,
        tags: list[str],
        source: str,
        metadata: dict,
    ) -> Optional[str]:
        """POST /api/v1/memory/ — returns memory id or None on failure."""
        if self.dry_run:
            return "dry-run-id"

        url = f"{self.base_url}/api/v1/memory/"
        payload = {
            "content": content,
            "namespace": namespace,
            "tags": tags,
            "source": source,
            "metadata": metadata,
        }
        resp = self._session.post(url, json=payload, timeout=30)
        resp.raise_for_status()
        return resp.json().get("id")

    def add_graph_fact(
        self,
        subject: str,
        predicate: str,
        obj: str,
        namespace: str,
    ) -> bool:
        """POST /api/v1/graph/fact — returns True on success."""
        if self.dry_run:
            return True

        url = f"{self.base_url}/api/v1/graph/fact"
        payload = {
            "subject": subject,
            "predicate": predicate,
            "object": obj,
            "namespace": namespace,
        }
        resp = self._session.post(url, json=payload, timeout=30)
        resp.raise_for_status()
        return True


# ---------------------------------------------------------------------------
# Migration logic
# ---------------------------------------------------------------------------


# Directories that are never Obsidian notes — skip them during vault walk.
_SKIP_DIRS = {
    ".obsidian",    # Obsidian app config
    ".git",         # git internals
    ".github",      # GitHub workflows/templates
    ".claude",      # Claude Code settings
    ".history",     # local edit history
    ".venv", "venv", "env",          # Python virtualenvs
    "node_modules",                   # JS dependencies
    "__pycache__",                    # Python bytecode
    "target",                         # Maven / Gradle build output
    "dist", "build", "out",           # generic build output
    "site-packages",                  # installed Python packages
}


def collect_notes(vault: Path, folder_filter: Optional[str]) -> list[Path]:
    """Walk vault directory and collect .md files, skipping non-note directories."""
    notes: list[Path] = []
    base = vault / folder_filter if folder_filter else vault

    for md_path in sorted(base.rglob("*.md")):
        if _SKIP_DIRS.intersection(md_path.parts):
            continue
        notes.append(md_path)

    return notes


def dry_run_report(notes: list[Path], vault: Path) -> None:
    """Parse notes and print a summary without writing anything."""
    tag_counts: dict[str, int] = {}
    total_wikilinks = 0
    total_notes = len(notes)

    for path in notes:
        parsed = parse_file(path)
        for tag in parsed["tags"]:
            tag_counts[tag] = tag_counts.get(tag, 0) + 1
        total_wikilinks += len(parsed["wikilinks"])

    print(f"\nDry-run report")
    print(f"  Notes found   : {total_notes}")
    print(f"  Total wikilinks: {total_wikilinks}")
    print(f"\n  Top 20 tags:")

    sorted_tags = sorted(tag_counts.items(), key=lambda x: -x[1])
    for tag, count in sorted_tags[:20]:
        print(f"    #{tag:<40} {count}")

    if not sorted_tags:
        print("    (none found)")

    print()


def migrate(args: argparse.Namespace) -> None:
    vault = Path(args.vault).expanduser().resolve()
    if not vault.is_dir():
        print(f"[error] Vault directory not found: {vault}", file=sys.stderr)
        sys.exit(1)

    client = EngramClient(
        base_url=args.engram_url,
        api_key=args.api_key,
        dry_run=args.dry_run,
    )

    notes = collect_notes(vault, args.folder)

    if args.limit:
        notes = notes[: args.limit]

    total = len(notes)

    if args.dry_run:
        print(f"[dry-run] Found {total} notes in {vault}")
        dry_run_report(notes, vault)
        return

    print(f"Migrating {total} notes from {vault}")
    print(f"  Namespace : {args.namespace}")
    print(f"  Target    : {args.engram_url}")
    print()

    # title (lowercase) → memory_id
    title_to_id: dict[str, str] = {}
    # title → list[wikilink targets]
    title_to_links: dict[str, list[str]] = {}
    # title → namespace (needed for graph edges)
    title_to_ns: dict[str, str] = {}

    ok_count = 0
    fail_count = 0

    for idx, note_path in enumerate(notes, start=1):
        try:
            parsed = parse_file(note_path)
        except Exception as exc:
            print(f"  [warn] Could not read {note_path}: {exc}")
            fail_count += 1
            continue

        namespace = note_namespace(vault, note_path, args.namespace)
        title = parsed["title"]
        title_key = title.lower()

        metadata: dict = {
            "obsidian_path": str(note_path.relative_to(vault)),
            "aliases": parsed["frontmatter"].get("aliases") or [],
        }

        # Carry any frontmatter date if present
        for date_key in ("date", "created", "modified", "updated"):
            if date_key in parsed["frontmatter"]:
                metadata[date_key] = str(parsed["frontmatter"][date_key])

        try:
            mem_id = client.write_memory(
                content=parsed["content"],
                namespace=namespace,
                tags=parsed["tags"],
                source=f"obsidian:{note_path.relative_to(vault)}",
                metadata=metadata,
            )
        except requests.HTTPError as exc:
            print(f"  [fail] {title}: HTTP {exc.response.status_code} — {exc.response.text[:120]}")
            fail_count += 1
        except Exception as exc:
            print(f"  [fail] {title}: {exc}")
            fail_count += 1
        else:
            if mem_id:
                title_to_id[title_key] = mem_id
                title_to_links[title_key] = parsed["wikilinks"]
                title_to_ns[title_key] = namespace
                ok_count += 1

                if args.verbose:
                    print(f"  [ok] {title} → {mem_id}")

        # Progress every 10 notes
        if idx % 10 == 0 or idx == total:
            print(f"  {idx}/{total} — {ok_count} ok, {fail_count} failed")

        # Rate limiting between batches
        if idx % args.batch_size == 0 and idx < total:
            time.sleep(0.5)

    # ------------------------------------------------------------------
    # Graph edges — wikilinks between successfully imported notes
    # ------------------------------------------------------------------
    print(f"\nAdding graph edges for wikilinks...")

    edge_ok = 0
    edge_skip = 0
    edge_fail = 0

    for src_title_key, wikilinks in title_to_links.items():
        src_id = title_to_id.get(src_title_key)
        if not src_id:
            continue

        for link_target in wikilinks:
            target_key = link_target.lower()
            tgt_id = title_to_id.get(target_key)
            if not tgt_id:
                edge_skip += 1
                continue

            ns = title_to_ns.get(src_title_key, args.namespace)
            try:
                client.add_graph_fact(
                    subject=src_id,
                    predicate="links_to",
                    obj=tgt_id,
                    namespace=ns,
                )
                edge_ok += 1
            except Exception as exc:
                if args.verbose:
                    print(f"  [edge-fail] {src_title_key} → {link_target}: {exc}")
                edge_fail += 1

    # ------------------------------------------------------------------
    # Final summary
    # ------------------------------------------------------------------
    print()
    print("=" * 60)
    print("Migration complete")
    print(f"  Notes written  : {ok_count}/{total}")
    print(f"  Notes failed   : {fail_count}")
    print(f"  Graph edges    : {edge_ok} added, {edge_skip} skipped (target not imported), {edge_fail} failed")
    print()
    print("Test curl command:")
    first_id = next(iter(title_to_id.values()), None)
    if first_id and first_id != "dry-run-id":
        print(
            f"  curl -s -H 'Authorization: Bearer {args.api_key}' \\\n"
            f"       {args.engram_url}/api/v1/memory/{first_id} | python3 -m json.tool"
        )
    else:
        print(
            f"  curl -s -H 'Authorization: Bearer {args.api_key}' \\\n"
            f"       '{args.engram_url}/api/v1/memory/?namespace={args.namespace}&limit=5' | python3 -m json.tool"
        )
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="migrate_obsidian",
        description="Migrate an Obsidian vault into the engram REST API.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--vault",
        required=True,
        metavar="PATH",
        help="Path to the Obsidian vault directory.",
    )
    parser.add_argument(
        "--namespace",
        required=True,
        metavar="NS",
        help="Base namespace for imported notes, e.g. obsidian:my-vault.",
    )
    parser.add_argument(
        "--engram-url",
        default="http://localhost:8766",
        metavar="URL",
        help="engram API base URL (default: http://localhost:8766).",
    )
    parser.add_argument(
        "--api-key",
        required=True,
        metavar="KEY",
        help="Bearer API key for engram authentication.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse vault and report without writing to engram.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=10,
        metavar="N",
        help="Sleep 0.5s after every N notes (rate limiting). Default: 10.",
    )
    parser.add_argument(
        "--folder",
        default=None,
        metavar="SUBPATH",
        help="Only migrate notes under this subfolder (relative to vault root).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Only migrate the first N notes (for testing).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print each note title as it is written.",
    )
    return parser


if __name__ == "__main__":
    parser = build_parser()
    args = parser.parse_args()
    migrate(args)
