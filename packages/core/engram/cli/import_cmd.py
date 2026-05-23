"""
engram-import — import a folder of markdown/text files into engram.

Usage
-----
  engram-import ~/vaults/myvault --namespace org:me:notes
  engram-import ~/vaults/myvault --namespace org:myorg --discover
  engram-import ~/vaults/myvault --namespace org:myorg --discover --dry-run

How namespace discovery works
------------------------------
1. Heuristic pass (free, no LLM):
   - Folder name → namespace segment  (rfi/ → :rfi, customers/ → :customers)
   - Filename prefix matching         (acme-*.md → folder's namespace)
   - Content keyword scan             ($, pricing, meeting, customer names)

2. Claude Code pass (--discover flag, uses your Claude Code subscription):
   - Ambiguous files are classified by calling `claude -p "..."` as a subprocess
   - No separate API key needed — uses your existing OAuth session
   - Results are cached per-folder so Claude is called once per folder, not per file

The discovered mapping is printed as a table before any writes happen.
Use --dry-run to see the mapping without importing anything.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_SERVER = "http://localhost:8766"
DEFAULT_SKIP = {
    "BINARY-FILES.md",
    ".git",
    "__pycache__",
    "node_modules",
    ".DS_Store",
}
DEFAULT_SKIP_PATTERNS = [
    r"^\..*",          # hidden files/dirs
    r".*\.pyc$",
    r".*\.egg-info",
]

# Folder names that map to known namespace suffixes (general patterns).
# Suffixes starting with "private:" hold sensitive/commercial content.
# Suffixes starting with "engineering:" hold technical content.
SEMANTIC_FOLDER_MAP: dict[str, str] = {
    # Private / commercial
    "rfi": "private:rfi",
    "rfp": "private:rfi",
    "pricing": "private:pricing",
    "strategy": "private:strategy",
    "competitive": "private:strategy",
    "customers": "private:customers",
    "context": "private:context",
    "sessions": "private:sessions",
    # Engineering / technical
    "security": "engineering:security",
    "hitrust": "engineering:security",
    "compliance": "engineering:security",
    "tools": "engineering:tools",
    "engineering": "engineering",
    "k8s": "engineering:k8s",
    "kubernetes": "engineering:k8s",
    "infra": "engineering:k8s",
    "aws": "engineering:k8s",
    "azure": "engineering:k8s",
    "sdlc": "engineering:sdlc",
}

# Content keywords that signal a namespace category (suffix relative to base_ns)
CONTENT_SIGNALS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\$[\d,]+|\bpricin|\bcost\b|\bestimate\b|\bbudget\b", re.I), "private:pricing"),
    (re.compile(r"\bRFI\b|\bRFP\b|\bproposal\b|\bresponse\b", re.I), "private:rfi"),
    (re.compile(r"\bHITRUST\b|\bSOC 2\b|\bHIPAA\b|\baudit\b|\bcompliance\b", re.I), "engineering:security"),
    (re.compile(r"\bKubernetes\b|\bAKS\b|\bdocker\b|\bHelm\b|\bpipeline\b", re.I), "engineering:k8s"),
    (re.compile(r"\bmeeting\b|\bcall notes\b|\bdiscovery\b|\bstakeholder\b", re.I), "private:sessions"),
]

# Files to skip entirely
SKIP_FILES = {
    "BINARY-FILES.md", "AGENTS.md", "CHANGELOG.md", "CONTRIBUTING.md",
    "README.md", ".gitignore", ".gitkeep",
}

SUPPORTED_EXTENSIONS = {".md", ".txt", ".rst", ".org"}


# ---------------------------------------------------------------------------
# Namespace discovery
# ---------------------------------------------------------------------------

def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "-", text.lower()).strip("-")


def _folder_namespace(rel_parts: list[str], base_ns: str) -> str:
    """Derive a namespace from folder path components using semantic mappings."""
    # Walk parts from deepest to shallowest, first match wins
    for part in reversed(rel_parts):
        part_lower = part.lower()
        for key, ns_suffix in SEMANTIC_FOLDER_MAP.items():
            if part_lower == key or part_lower.startswith(key):
                # Check if it looks like a customer folder inside learnings/customers
                parent_idx = rel_parts.index(part)
                parent = rel_parts[parent_idx - 1].lower() if parent_idx > 0 else ""
                if parent in ("learnings", "context") and ns_suffix not in SEMANTIC_FOLDER_MAP.values():
                    return f"{base_ns}:private:customers:{_slug(part)}"
                return f"{base_ns}:{ns_suffix}"

    # Not a known semantic folder — treat as customer name if parent is learnings/context
    if len(rel_parts) >= 2:
        parent = rel_parts[-2].lower() if len(rel_parts) >= 2 else ""
        if parent in ("learnings", "context", "customers"):
            return f"{base_ns}:private:customers:{_slug(rel_parts[-1])}"
        # Otherwise build namespace from folder path
        meaningful = [p for p in rel_parts if p.lower() not in ("memory", "learnings", "context")]
        if meaningful:
            return f"{base_ns}:{':'.join(_slug(p) for p in meaningful)}"

    # Single unknown folder directly under root → treat as customer subdirectory.
    # Vault structure is typically learnings/{customer}/files.md — any single-level
    # folder not matched by SEMANTIC_FOLDER_MAP is a customer or product name.
    if len(rel_parts) == 1:
        part = rel_parts[0].lower()
        if part not in SEMANTIC_FOLDER_MAP:
            return f"{base_ns}:private:customers:{_slug(part)}"

    return base_ns


def _content_namespace(content: str, base_ns: str) -> str | None:
    """Classify content by keyword signals. Returns a namespace suffix or None."""
    preview = content[:800]
    for pattern, suffix in CONTENT_SIGNALS:
        if pattern.search(preview):
            return f"{base_ns}:{suffix}"
    return None


def _filename_namespace(
    filename: str, folder_ns: str, base_ns: str,
    customer_names: frozenset[str] = frozenset(),
) -> str:
    """Check filename prefix against known customer/topic patterns.

    ``customer_names`` is discovered at runtime from the directory structure —
    any subfolder name not in SEMANTIC_FOLDER_MAP is assumed to be a customer.
    """
    stem = Path(filename).stem.lower()

    # Customer prefix detection using runtime-discovered customer folder names.
    # Longest match first to avoid "ac" matching "acme-extra" before "acme-something-long".
    for customer in sorted(customer_names, key=len, reverse=True):
        if stem == customer or stem.startswith(customer + "-"):
            mapped = SEMANTIC_FOLDER_MAP.get(customer)
            if mapped:
                return f"{base_ns}:{mapped}"
            return f"{base_ns}:private:customers:{customer}"

    # RFI/pricing filename signals
    if any(k in stem for k in ("rfi", "rfp", "proposal", "response")):
        return f"{base_ns}:private:rfi"
    if any(k in stem for k in ("pricing", "cost", "estimate", "budget")):
        return f"{base_ns}:private:pricing"
    if "azure" in stem or "aks" in stem:
        return f"{base_ns}:engineering:k8s"
    return folder_ns


def _classify_with_claude(
    files_preview: list[dict],
    base_ns: str,
    available_ns: list[str],
) -> dict[str, str]:
    """
    Call `claude -p` to classify ambiguous files.
    files_preview: list of {path: str, content_preview: str}
    Returns: {path: namespace}
    """
    ns_list = "\n".join(f"  - {ns}" for ns in available_ns)
    file_list = "\n\n".join(
        f"File: {f['path']}\nPreview: {f['content_preview'][:300]}"
        for f in files_preview
    )

    prompt = f"""You are classifying vault files into namespaces for an engram memory system.

Base namespace: {base_ns}
Available namespaces (choose the best fit or suggest a new one following the same pattern):
{ns_list}

Files to classify:
{file_list}

Reply with ONLY a JSON object mapping each file path to its namespace.
Example: {{"path/to/file.md": "{base_ns}:customers:acme"}}
No explanation, just the JSON."""

    claude_bin = _find_claude()
    if not claude_bin:
        return {}

    try:
        result = subprocess.run(
            [claude_bin, "-p", prompt, "--output-format", "text"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        output = result.stdout.strip()
        # Extract JSON from output
        json_match = re.search(r"\{.*\}", output, re.DOTALL)
        if json_match:
            return json.loads(json_match.group())
    except Exception as e:
        logger.debug("Claude classification failed: %s", e)
    return {}


def _find_claude() -> str | None:
    """Find the claude CLI binary."""
    for candidate in [
        os.path.expanduser("~/.local/bin/claude"),
        "/usr/local/bin/claude",
        "/opt/homebrew/bin/claude",
    ]:
        if Path(candidate).exists():
            return candidate
    import shutil
    return shutil.which("claude")


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

def _should_skip(path: Path) -> bool:
    name = path.name
    if name in SKIP_FILES or name in DEFAULT_SKIP:
        return True
    for pattern in DEFAULT_SKIP_PATTERNS:
        if re.match(pattern, name):
            return True
    if path.is_file() and path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        return True
    return False


def _iter_files(root: Path) -> Iterator[Path]:
    for path in sorted(root.rglob("*")):
        if path.is_file() and not _should_skip(path):
            # Skip deeply nested project files (e.g. embedded source code repos)
            rel = path.relative_to(root)
            if len(rel.parts) > 6:
                continue
            yield path


# ---------------------------------------------------------------------------
# Mapping builder
# ---------------------------------------------------------------------------

def build_mapping(
    root: Path,
    base_ns: str,
    use_claude: bool = False,
    skip_dirs: list[str] | None = None,
) -> dict[Path, str]:
    """
    Scan root and return {file_path: namespace} mapping.
    """
    skip_set = set(skip_dirs or [])
    files = [f for f in _iter_files(root) if not any(s in str(f) for s in skip_set)]

    # Discover customer subfolders at depth 1: any folder whose name is not in
    # SEMANTIC_FOLDER_MAP is assumed to be a customer/product name.
    customer_names: frozenset[str] = frozenset(
        d.name.lower()
        for d in root.iterdir()
        if d.is_dir() and not d.name.startswith(".")
    )

    mapping: dict[Path, str] = {}
    ambiguous: list[dict] = []

    for filepath in files:
        rel = filepath.relative_to(root)
        folder_parts = list(rel.parts[:-1])

        # Step 1: folder-based namespace
        ns = _folder_namespace(folder_parts, base_ns) if folder_parts else base_ns

        # Step 2: filename refinement — pass discovered customer names for prefix matching
        ns = _filename_namespace(filepath.name, ns, base_ns, customer_names=customer_names)

        # Step 3: content signals (for files that are still at base_ns)
        if ns == base_ns:
            try:
                content = filepath.read_text(errors="replace")[:800]
                content_ns = _content_namespace(content, base_ns)
                if content_ns:
                    ns = content_ns
                elif use_claude:
                    ambiguous.append({
                        "path": str(rel),
                        "content_preview": content[:300],
                        "_filepath": filepath,
                    })
            except Exception:
                pass

        mapping[filepath] = ns

    # Step 4: Claude classification for ambiguous files
    if use_claude and ambiguous:
        existing_ns = sorted(set(mapping.values()))
        print(f"\n  Using Claude Code to classify {len(ambiguous)} ambiguous file(s)...")
        claude_map = _classify_with_claude(
            [{"path": f["path"], "content_preview": f["content_preview"]} for f in ambiguous],
            base_ns,
            existing_ns,
        )
        for item in ambiguous:
            filepath = item["_filepath"]
            suggested = claude_map.get(item["path"])
            if suggested and suggested.startswith(base_ns):
                mapping[filepath] = suggested

    return mapping


# ---------------------------------------------------------------------------
# Import writer
# ---------------------------------------------------------------------------

async def _write_memory(
    client: httpx.AsyncClient,
    server: str,
    api_key: str,
    content: str,
    namespace: str,
    source_path: str,
    tags: list[str],
) -> tuple[bool, str]:
    try:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        resp = await client.post(
            f"{server}/api/v1/memory/",
            headers=headers,
            json={
                "content": content,
                "namespace": namespace,
                "source": "vault-import",
                "tags": tags,
                "metadata": {"source_file": source_path},
            },
            timeout=120.0,
        )
        if resp.status_code in (200, 201):
            return True, resp.json().get("id", "")
        return False, f"HTTP {resp.status_code}: {resp.text[:100]}"
    except Exception as e:
        return False, str(e)


async def run_import(
    root: Path,
    base_ns: str,
    server: str,
    api_key: str,
    dry_run: bool,
    use_claude: bool,
    skip_dirs: list[str],
    batch_size: int,
    tags: list[str],
) -> None:
    print(f"\n  Scanning {root} ...")
    mapping = build_mapping(root, base_ns, use_claude=use_claude, skip_dirs=skip_dirs)

    if not mapping:
        print("  No supported files found.")
        return

    # Group by namespace for display
    ns_groups: dict[str, list[Path]] = {}
    for fp, ns in mapping.items():
        ns_groups.setdefault(ns, []).append(fp)

    print(f"\n  Discovered namespace mapping ({len(mapping)} files → {len(ns_groups)} namespaces):\n")
    col_w = max(len(ns) for ns in ns_groups) + 2
    print(f"  {'Namespace':<{col_w}}  Files")
    print(f"  {'-' * col_w}  -----")
    for ns in sorted(ns_groups):
        files = ns_groups[ns]
        sample = ", ".join(f.name for f in files[:3])
        suffix = f" (+{len(files)-3} more)" if len(files) > 3 else ""
        print(f"  {ns:<{col_w}}  {len(files):>3}  {sample}{suffix}")

    if dry_run:
        print("\n  [dry-run] No files were written. Remove --dry-run to import.\n")
        return

    print(f"\n  Importing {len(mapping)} files (batch_size={batch_size})...")
    ok = err = 0
    items = list(mapping.items())

    async with httpx.AsyncClient() as client:
        for i in range(0, len(items), batch_size):
            batch = items[i : i + batch_size]
            tasks = []
            for filepath, ns in batch:
                try:
                    content = filepath.read_text(errors="replace")
                    if not content.strip():
                        continue
                    rel_str = str(filepath.relative_to(root))
                    file_tags = tags + [Path(rel_str).stem]
                    tasks.append(
                        _write_memory(client, server, api_key, content, ns, rel_str, file_tags)
                    )
                except Exception as e:
                    logger.debug("Read error %s: %s", filepath, e)

            results = await asyncio.gather(*tasks, return_exceptions=True)
            for (fp, ns), result in zip(batch, results):
                if isinstance(result, Exception) or (isinstance(result, tuple) and not result[0]):
                    err += 1
                    detail = result[1] if isinstance(result, tuple) else str(result)
                    logger.debug("  FAIL %s: %s", fp.name, detail)
                else:
                    ok += 1

            done = min(i + batch_size, len(items))
            pct = int(done / len(items) * 40)
            bar = "█" * pct + "░" * (40 - pct)
            print(f"\r  [{bar}] {done}/{len(items)}  ok={ok} err={err}", end="", flush=True)
            await asyncio.sleep(0.1)

    print(f"\n\n  Done. {ok} imported, {err} failed.\n")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        prog="engram-import",
        description="Import a folder of markdown/text files into engram with auto namespace discovery.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  engram-import ~/vaults/myvault --namespace org:me:notes
  engram-import ~/vaults/myvault --namespace org:myorg --discover
  engram-import ~/vaults/myvault --namespace org:myorg --discover --dry-run
  engram-import ~/notes --namespace org:me --skip projects --skip .git
        """,
    )
    parser.add_argument("path", help="Folder to import")
    parser.add_argument("--namespace", "-n", required=True, help="Base namespace (e.g. org:myorg)")
    parser.add_argument(
        "--discover", "-d", action="store_true",
        help="Use Claude Code CLI to classify ambiguous files (uses your Claude Code subscription, no API key needed)"
    )
    parser.add_argument("--dry-run", action="store_true", help="Show mapping without importing")
    parser.add_argument("--server", default=os.environ.get("ENGRAM_SERVER", DEFAULT_SERVER),
                        help=f"engram server URL (default: {DEFAULT_SERVER})")
    parser.add_argument("--api-key", default=os.environ.get("ENGRAM_API_KEY", ""),
                        help="engram API key (default: ENGRAM_API_KEY env var)")
    parser.add_argument("--skip", action="append", default=[], metavar="DIR",
                        help="Skip directories matching this name (repeatable)")
    parser.add_argument("--batch-size", type=int, default=3,
                        help="Concurrent writes per batch (default: 3)")
    parser.add_argument("--tags", default="vault-import",
                        help="Comma-separated tags to apply to all imported memories")
    parser.add_argument("--verbose", "-v", action="store_true")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )

    root = Path(args.path).expanduser().resolve()
    if not root.exists():
        print(f"Error: path does not exist: {root}", file=sys.stderr)
        sys.exit(1)

    if not args.api_key and not args.dry_run:
        print("  Note: no --api-key set. Proceeding without auth (works if server runs in open_mode).")
        print("        Set ENGRAM_API_KEY or pass --api-key if the server requires authentication.\n")

    tags = [t.strip() for t in args.tags.split(",") if t.strip()]

    if args.discover:
        claude_bin = _find_claude()
        if claude_bin:
            print(f"  Claude Code found at {claude_bin} — will use for classification")
        else:
            print("  Warning: --discover requested but claude CLI not found. Falling back to heuristics.")
            args.discover = False

    asyncio.run(run_import(
        root=root,
        base_ns=args.namespace,
        server=args.server,
        api_key=args.api_key,
        dry_run=args.dry_run,
        use_claude=args.discover,
        skip_dirs=args.skip,
        batch_size=args.batch_size,
        tags=tags,
    ))


if __name__ == "__main__":
    main()
