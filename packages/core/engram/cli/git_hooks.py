"""
engram.cli.git_hooks — engram-git CLI: install and run git hooks.

Commands
--------
engram-git install [--repo PATH] [--server URL]
    Install post-commit and pre-review hooks into a git repository.

engram-git post-commit [--repo PATH] [--server URL] [--namespace NS]
    Run the post-commit hook manually (also called by the installed hook).

engram-git pre-review [--diff PATH] [--server URL] [--namespace NS]
    Given a diff file or stdin, retrieve relevant engram memories as context.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
import textwrap
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_SERVER = os.environ.get("ENGRAM_SERVER_URL", "http://localhost:8766")
_DEFAULT_NAMESPACE = os.environ.get("ENGRAM_NAMESPACE", "org:default")
_HOOK_TEMPLATE = textwrap.dedent(
    """\
    #!/bin/sh
    # engram post-commit hook — writes a memory for every commit
    # Installed by: engram-git install
    engram-git post-commit --server {server} --namespace {namespace}
    """
)
_INCIDENT_HOOK_TEMPLATE = textwrap.dedent(
    """\
    #!/bin/sh
    # engram post-merge hook — records incident RCA when an incident branch is merged
    # Installed by: engram-git install --incident
    engram-git post-merge-incident --server {server} --namespace {namespace}
    """
)

# Branch-name patterns that indicate an incident/hotfix merge
_INCIDENT_BRANCH_PATTERNS = (
    "incident/", "rca/", "hotfix/", "fix/incident", "fix/inc-",
    "inc/", "postmortem/", "pm/",
)

# RCA section headers (lowercase) → dict key
_RCA_HEADERS = {
    "root cause": "root_cause",
    "root_cause": "root_cause",
    "resolution": "resolution",
    "fix": "resolution",
    "impact": "impact",
    "affected services": "affected_services",
    "services": "affected_services",
    "severity": "severity",
    "timeline": "timeline",
    "action items": "action_items",
    "follow-up": "action_items",
}


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------

def cmd_install(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    git_dir = repo / ".git"
    if not git_dir.is_dir():
        print(f"ERROR: {repo} is not a git repository (no .git directory found)", file=sys.stderr)
        return 1

    hooks_dir = git_dir / "hooks"
    hooks_dir.mkdir(exist_ok=True)

    hook_path = hooks_dir / "post-commit"
    content = _HOOK_TEMPLATE.format(server=args.server, namespace=args.namespace)
    hook_path.write_text(content)
    hook_path.chmod(0o755)
    print(f"✓ Installed post-commit hook → {hook_path}")
    print(f"  server: {args.server}  namespace: {args.namespace}")
    print(f"  Every commit will write a memory to engram automatically.")

    if getattr(args, "incident", False):
        inc_path = hooks_dir / "post-merge"
        inc_content = _INCIDENT_HOOK_TEMPLATE.format(server=args.server, namespace=args.namespace)
        inc_path.write_text(inc_content)
        inc_path.chmod(0o755)
        print(f"✓ Installed post-merge (incident) hook → {inc_path}")
        print(f"  Incident/hotfix branch merges will auto-record RCA memories.")
    return 0


# ---------------------------------------------------------------------------
# Post-commit hook
# ---------------------------------------------------------------------------

def _git_commit_info(repo: Path) -> dict:
    """Extract metadata from the last commit."""
    def git(*cmd: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(repo), *cmd], text=True, stderr=subprocess.DEVNULL
        ).strip()

    try:
        sha = git("rev-parse", "--short", "HEAD")
        author = git("log", "-1", "--format=%an <%ae>")
        message = git("log", "-1", "--format=%B")
        files = git("diff-tree", "--no-commit-id", "-r", "--name-only", "HEAD").splitlines()
        branch = git("rev-parse", "--abbrev-ref", "HEAD")
        return {
            "sha": sha,
            "author": author,
            "message": message.strip(),
            "files": files[:30],  # cap at 30 files for memory content
            "branch": branch,
        }
    except subprocess.CalledProcessError:
        return {}


def cmd_post_commit(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    info = _git_commit_info(repo)
    if not info:
        logger.warning("Could not extract commit info; hook skipped")
        return 0

    files_str = "\n".join(f"  - {f}" for f in info["files"])
    content = (
        f"Git commit: {info['sha']} on branch {info['branch']}\n"
        f"Author: {info['author']}\n"
        f"Message: {info['message']}\n"
        f"Changed files:\n{files_str}"
    )

    api_key = os.environ.get("ENGRAM_API_KEY", "")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = {
        "content": content,
        "namespace": args.namespace,
        "tags": ["git-commit", info["branch"]],
        "source": "git-hook",
        "memory_type": "fact",
        "author": info["author"],
        "metadata": {"sha": info["sha"], "branch": info["branch"]},
    }

    return asyncio.run(_post_memory(args.server, headers, payload))


async def _post_memory(server: str, headers: dict, payload: dict) -> int:
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{server}/api/v1/memory/",
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
            print(f"engram: commit memory written (id: {data.get('id', '?')})", file=sys.stderr)
            return 0
    except Exception as exc:
        # Non-fatal — git commits should never fail due to engram being down
        print(f"engram: post-commit hook skipped ({exc})", file=sys.stderr)
        return 0


# ---------------------------------------------------------------------------
# Post-merge incident hook
# ---------------------------------------------------------------------------

def _is_incident_branch(branch_name: str) -> bool:
    """Return True when the branch name matches a known incident naming pattern."""
    lower = branch_name.lower()
    return any(lower.startswith(p) or p in lower for p in _INCIDENT_BRANCH_PATTERNS)


def _parse_rca_content(text: str) -> dict:
    """
    Extract structured RCA fields from free-form merge commit body text.

    Recognises both Markdown heading style (## Root Cause) and inline label
    style (root_cause: ...).  Returns a dict with any recognised fields plus
    the full raw text under key ``full_text``.
    """
    parsed: dict[str, str] = {"full_text": text.strip()}
    if not text.strip():
        return parsed

    lines = text.splitlines()
    current_key: str | None = None
    buffer: list[str] = []

    def _flush() -> None:
        if current_key and buffer:
            parsed[current_key] = " ".join(" ".join(buffer).split())  # normalise whitespace

    for line in lines:
        stripped = line.strip()
        # Markdown heading: ## Root Cause
        heading = stripped.lstrip("#").strip().lower()
        if stripped.startswith("#") and heading in _RCA_HEADERS:
            _flush()
            current_key = _RCA_HEADERS[heading]
            buffer = []
            continue

        # Inline label: severity: P1  (transition even mid-section)
        if ":" in stripped:
            label, _, rest = stripped.partition(":")
            label_lower = label.strip().lower()
            if label_lower in _RCA_HEADERS and len(label.strip().split()) <= 3:
                _flush()
                current_key = _RCA_HEADERS[label_lower]
                buffer = [rest.strip()] if rest.strip() else []
                continue

        if current_key is not None:
            if stripped.startswith("#") or (stripped and stripped == stripped.upper() and len(stripped) > 3):
                # New section started — flush old
                _flush()
                current_key = None
                buffer = []
            else:
                if stripped:
                    buffer.append(stripped)

    _flush()
    return parsed


def _get_merged_branch(repo: Path) -> str:
    """Return the name of the branch that was just merged, if determinable."""
    def git(*cmd: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(repo), *cmd], text=True, stderr=subprocess.DEVNULL
        ).strip()

    # The merge commit subject usually reads: "Merge branch 'incident/foo' into main"
    try:
        subject = git("log", "-1", "--format=%s")
        import re as _re
        m = _re.search(r"Merge branch ['\"]([^'\"]+)['\"]", subject)
        if m:
            return m.group(1)
    except subprocess.CalledProcessError:
        pass

    # Fall back to ORIG_HEAD comparison
    try:
        merged = git("log", "--merges", "-1", "--format=%P").split()
        if len(merged) >= 2:
            branch_ref = git("name-rev", "--name-only", merged[1])
            return branch_ref.split("~")[0].split("^")[0]
    except subprocess.CalledProcessError:
        pass

    return ""


def cmd_post_merge_incident(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()

    def git(*cmd: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(repo), *cmd], text=True, stderr=subprocess.DEVNULL
        ).strip()

    try:
        merged_branch = _get_merged_branch(repo)
        current_branch = git("rev-parse", "--abbrev-ref", "HEAD")
        sha = git("rev-parse", "--short", "HEAD")
        author = git("log", "-1", "--format=%an <%ae>")
        body = git("log", "-1", "--format=%B")
    except subprocess.CalledProcessError as exc:
        logger.warning("Could not read git info for incident hook: %s", exc)
        return 0

    if not merged_branch or not _is_incident_branch(merged_branch):
        # Not an incident branch — hook is a no-op
        return 0

    rca = _parse_rca_content(body)

    severity = rca.get("severity", "").upper() or "UNKNOWN"
    root_cause = rca.get("root_cause", "")
    resolution = rca.get("resolution", "")
    impact = rca.get("impact", "")
    affected_services = rca.get("affected_services", "")

    content_lines = [
        f"Incident RCA: merged branch {merged_branch!r} into {current_branch}",
        f"Commit: {sha}  Author: {author}",
        f"Severity: {severity}",
    ]
    if affected_services:
        content_lines.append(f"Affected services: {affected_services}")
    if root_cause:
        content_lines.append(f"Root cause: {root_cause}")
    if resolution:
        content_lines.append(f"Resolution: {resolution}")
    if impact:
        content_lines.append(f"Impact: {impact}")
    if not any([root_cause, resolution, impact]):
        # No structured sections found — store the raw body
        content_lines.append(f"Details:\n{rca['full_text'][:2000]}")

    content = "\n".join(content_lines)
    tags = ["incident", "rca", merged_branch.replace("/", "-")]
    if severity not in ("UNKNOWN", ""):
        tags.append(severity.lower())

    api_key = os.environ.get("ENGRAM_API_KEY", "")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = {
        "content": content,
        "namespace": args.namespace,
        "tags": tags,
        "source": "git-hook",
        "memory_type": "incident",
        "author": author,
        "metadata": {"sha": sha, "merged_branch": merged_branch, "severity": severity},
    }

    rc = asyncio.run(_post_memory(args.server, headers, payload))
    if rc == 0:
        print(
            f"engram: incident RCA memory written for {merged_branch!r} (severity: {severity})",
            file=sys.stderr,
        )
    return rc


# ---------------------------------------------------------------------------
# Pre-review (retrieve relevant memories for a diff)
# ---------------------------------------------------------------------------

def cmd_pre_review(args: argparse.Namespace) -> int:
    if args.diff and args.diff != "-":
        diff_text = Path(args.diff).read_text(errors="replace")
    else:
        diff_text = sys.stdin.read()

    if not diff_text.strip():
        print("No diff content provided", file=sys.stderr)
        return 1

    # Extract file paths from the diff to build a targeted query
    changed_files = [
        line[6:]  # strip "--- a/" or "+++ b/"
        for line in diff_text.splitlines()
        if line.startswith(("--- a/", "+++ b/")) and not line.endswith("/dev/null")
    ]
    file_names = list({Path(f).name for f in changed_files if f})[:10]
    query = f"architecture decisions patterns constraints for files: {' '.join(file_names)}"

    api_key = os.environ.get("ENGRAM_API_KEY", "")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    results = asyncio.run(_search_memories(args.server, headers, args.namespace, query))
    if results:
        print("\n=== engram: relevant memories for this diff ===")
        print(results)
        print("=================================================\n")
    return 0


async def _search_memories(server: str, headers: dict, namespace: str, query: str) -> str:
    try:
        import httpx
        params = {"q": query, "ns": namespace, "top_k": 5, "mode": "hybrid"}
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{server}/api/v1/memory/search",
                params=params,
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
            results = data if isinstance(data, list) else data.get("results", [])
            if not results:
                return ""
            lines = [f"Found {len(results)} relevant memories:\n"]
            for i, r in enumerate(results[:5], 1):
                m = r.get("memory", r)
                content = str(m.get("content", ""))[:300]
                mem_type = m.get("memory_type", "fact")
                lines.append(f"{i}. [{mem_type}] {content}")
            return "\n".join(lines)
    except Exception as exc:
        logger.debug("pre-review memory search skipped: %s", exc)
        return ""


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="engram-git",
        description="engram git integration — install hooks and surface memories during development",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # install
    p_install = sub.add_parser("install", help="Install git hooks into a repository")
    p_install.add_argument("--repo", default=".", help="Path to git repository (default: .)")
    p_install.add_argument("--server", default=_DEFAULT_SERVER, help="Engram server URL")
    p_install.add_argument("--namespace", default=_DEFAULT_NAMESPACE, help="Target namespace")
    p_install.add_argument(
        "--incident", action="store_true",
        help="Also install post-merge hook for automatic incident RCA recording",
    )

    # post-commit
    p_post = sub.add_parser("post-commit", help="Run the post-commit hook (called by git)")
    p_post.add_argument("--repo", default=".", help="Path to git repository")
    p_post.add_argument("--server", default=_DEFAULT_SERVER, help="Engram server URL")
    p_post.add_argument("--namespace", default=_DEFAULT_NAMESPACE, help="Target namespace")

    # post-merge-incident
    p_merge = sub.add_parser(
        "post-merge-incident",
        help="Run the post-merge incident hook (called by git after merging an incident branch)",
    )
    p_merge.add_argument("--repo", default=".", help="Path to git repository")
    p_merge.add_argument("--server", default=_DEFAULT_SERVER, help="Engram server URL")
    p_merge.add_argument("--namespace", default=_DEFAULT_NAMESPACE, help="Target namespace")

    # pre-review
    p_review = sub.add_parser("pre-review", help="Retrieve relevant memories for a diff")
    p_review.add_argument("--diff", default="-", help="Diff file path (default: stdin)")
    p_review.add_argument("--server", default=_DEFAULT_SERVER, help="Engram server URL")
    p_review.add_argument("--namespace", default=_DEFAULT_NAMESPACE, help="Target namespace")

    args = parser.parse_args()

    dispatch = {
        "install": cmd_install,
        "post-commit": cmd_post_commit,
        "post-merge-incident": cmd_post_merge_incident,
        "pre-review": cmd_pre_review,
    }
    sys.exit(dispatch[args.command](args))


if __name__ == "__main__":
    main()
