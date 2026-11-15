"""Git-anchored provenance (`code_anchors`) & recall-time code drift detection (issue #239).

Why client-attached git blob SHAs + identifiers:
- The memnos server may run in Docker or remote SaaS without access to the developer's
  working tree. Clients (CLI, MCP, hooks) extract `(file, blob_sha, identifiers)` locally
  and attach `code_anchors` on `/remember`.
- Git blob SHAs are computed in-process (`sha1("blob <len>\\0<bytes>")` — byte-identical
  to `git hash-object` with zero subprocess overhead).
- At recall time, `check_code_drift` / `annotate_rows_with_code_drift` compares each
  anchored `(file, blob_sha, identifiers)` against the current working tree:
  - File deleted -> drifted (`deleted`)
  - `blob_sha` unchanged -> fresh (not drifted)
  - `blob_sha` changed -> if `identifiers` are anchored, checks `git diff -U0 <old_blob>`
    (when `<old_blob>` is in the git object DB) or identifier presence in the file so
    unrelated edits elsewhere in a large file do not false-positive.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path

_FILE_PATH_RE = re.compile(
    r"(?:^|[\s`'\"(\[{])"
    r"((?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\."
    r"(?:py|ts|tsx|js|jsx|go|rs|sql|sh|md|json|yaml|yml|toml|html|css|c|cc|cpp|h|hpp|java|rb))"
    r"(?=$|[\s`'\":),\]}.;])"
)
_BACKTICK_IDENT_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_.]{2,60})`")
_FUNC_CALL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]{2,50})\s*\(")
_QUALIFIED_IDENT_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]{2,40}\.[A-Za-z_][A-Za-z0-9_]{2,40})\b")
_SNAKE_OR_CAMEL_RE = re.compile(
    r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+|[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+)\b"
)

_STOP_IDENTS = {
    "true", "false", "none", "null", "self", "cls", "return", "import", "from",
    "class", "def", "async", "await", "function", "const", "let", "var", "print",
    "dict", "list", "tuple", "set", "str", "int", "float", "bool", "bytes",
}


def compute_git_blob_sha(file_path: str | Path) -> str | None:
    """Compute the exact git blob SHA-1 (`sha1("blob <size>\\0<bytes>")`) in pure Python."""
    try:
        data = Path(file_path).read_bytes()
    except OSError:
        return None
    header = f"blob {len(data)}\x00".encode("ascii")
    return hashlib.sha1(header + data).hexdigest()


def extract_identifiers_from_text(text: str, *, max_identifiers: int = 12) -> list[str]:
    """Extract code identifiers (functions, classes, snake_case, camelCase) from text."""
    s = text or ""
    found: list[str] = []
    seen: set[str] = set()

    for pat in (_BACKTICK_IDENT_RE, _FUNC_CALL_RE, _QUALIFIED_IDENT_RE, _SNAKE_OR_CAMEL_RE):
        for m in pat.finditer(s):
            ident = m.group(1).strip(".")
            # Skip file names matched as qualified identifiers (e.g. service.py)
            if re.search(r"\.(py|ts|tsx|js|jsx|go|rs|sql|sh|md|json|yaml|yml|toml|html|css)$", ident, re.I):
                continue
            low = ident.lower()
            if low in _STOP_IDENTS or len(ident) < 3:
                continue
            # If qualified like Class.method, also keep the leaf symbol name for file matching
            base = ident.split(".")[-1]
            for candidate in (ident, base):
                if len(candidate) >= 3 and candidate.lower() not in _STOP_IDENTS and candidate not in seen:
                    seen.add(candidate)
                    found.append(candidate)
                    if len(found) >= max_identifiers:
                        return found
    return found


def _clean_rel_path(raw: str | None) -> str | None:
    """Normalize a repository-relative path without mangling dot-directories (`.github/...`)
    or accepting path traversal (`../...`) or absolute paths (`/etc/...`)."""
    p = str(raw or "").strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    if not p or p.startswith("/"):
        return None
    parts = [seg for seg in p.split("/") if seg]
    if not parts or any(seg in (".", "..") for seg in parts):
        return None
    return "/".join(parts)


def extract_code_anchors(
    text: str,
    repo_root: str | Path | None = None,
    *,
    max_anchors: int = 8,
) -> list[dict]:
    """Extract `(file, blob_sha, identifiers)` anchors for files mentioned in `text`
    that exist under `repo_root` (defaults to CWD).
    """
    if not text:
        return []
    root = Path(repo_root or os.getcwd()).resolve()
    candidates: list[str] = []
    seen_paths: set[str] = set()

    for m in _FILE_PATH_RE.finditer(text):
        raw_path = _clean_rel_path(m.group(1))
        if not raw_path or raw_path in seen_paths:
            continue
        seen_paths.add(raw_path)
        candidates.append(raw_path)

    if not candidates:
        return []

    all_idents = extract_identifiers_from_text(text)
    anchors: list[dict] = []

    for rel in candidates:
        fpath = (root / rel).resolve()
        try:
            rel_norm = fpath.relative_to(root).as_posix()
        except ValueError:
            continue
        if not fpath.is_file():
            continue
        blob_sha = compute_git_blob_sha(fpath)
        if not blob_sha:
            continue
        try:
            file_text = fpath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            file_text = ""

        # Filter identifiers to those that actually appear in this file (or mentioned alongside it)
        matched_idents = [
            ident for ident in all_idents
            if "." not in ident and re.search(rf"\b{re.escape(ident)}\b", file_text)
        ][:8]

        anchors.append({
            "file": rel_norm,
            "blob_sha": blob_sha,
            "identifiers": matched_idents,
        })
        if len(anchors) >= max_anchors:
            break

    return anchors


def normalize_code_anchors(anchors) -> list[dict] | None:
    """Validate and normalize client-supplied `code_anchors` before DB storage."""
    if not anchors or not isinstance(anchors, list):
        return None
    clean: list[dict] = []
    seen: set[str] = set()
    for item in anchors[:16]:
        if not isinstance(item, dict):
            continue
        fpath = _clean_rel_path(item.get("file"))
        blob_sha = str(item.get("blob_sha") or "").strip().lower()
        if not fpath or len(fpath) > 300 or not re.fullmatch(r"[0-9a-f]{7,64}", blob_sha):
            continue
        if fpath in seen:
            continue
        seen.add(fpath)
        raw_idents = item.get("identifiers") or []
        idents = []
        if isinstance(raw_idents, list):
            for x in raw_idents[:12]:
                sx = str(x or "").strip()
                if sx and len(sx) <= 80:
                    idents.append(sx)
        clean.append({
            "file": fpath,
            "blob_sha": blob_sha,
            "identifiers": idents,
        })
    return clean or None


def _git_diff_touches_identifiers(
    repo_root: Path,
    rel_file: str,
    old_blob_sha: str,
    identifiers: list[str],
    current_text: str,
) -> tuple[bool, list[str]]:
    """Check whether changes between `old_blob_sha` and the current working-tree file
    touch any of `identifiers`.

    1. If any anchored identifier is no longer present in `current_text`, it definitely drifted.
    2. If `old_blob_sha` exists in the local git object database, inspect `git diff -U3 <old_blob>`
       against the current file content to see if any hunk touches an anchored identifier.
    3. If `old_blob_sha` is not in the git object DB (e.g. recorded from an uncommitted buffer),
       treat a blob SHA mismatch as drifted.
    """
    missing = [
        ident for ident in identifiers
        if not re.search(rf"\b{re.escape(ident)}\b", current_text)
    ]
    if missing:
        return True, missing

    # Try git cat-file -p <old_blob_sha> to compare hunks
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "cat-file", "-p", old_blob_sha],
            capture_output=True, text=True, timeout=2, check=False,
        )
    except Exception:
        proc = None

    if proc is None or proc.returncode != 0:
        # Old blob not in git object DB; since blob_sha changed, conservatively report drift
        return True, list(identifiers)

    old_lines = proc.stdout.splitlines()
    new_lines = current_text.splitlines()
    import difflib

    def _enclosing_header(lines: list[str], idx: int) -> str:
        for j in range(min(idx, len(lines) - 1), -1, -1):
            ln = lines[j]
            if re.match(r"^\s*(?:async\s+def|def|class|function|func|fn)\s+[A-Za-z_]", ln):
                return ln
        return ""

    sm = difflib.SequenceMatcher(None, old_lines, new_lines)
    impacted_chunks: list[str] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        lo_old = max(0, i1 - 2)
        hi_old = min(len(old_lines), i2 + 2)
        lo_new = max(0, j1 - 2)
        hi_new = min(len(new_lines), j2 + 2)
        impacted_chunks.extend(old_lines[lo_old:hi_old])
        impacted_chunks.extend(new_lines[lo_new:hi_new])
        if old_lines:
            impacted_chunks.append(_enclosing_header(old_lines, i1))
        if new_lines:
            impacted_chunks.append(_enclosing_header(new_lines, j1))

    changed_text = "\n".join(impacted_chunks)
    touched: list[str] = []
    for ident in identifiers:
        if re.search(rf"\b{re.escape(ident)}\b", changed_text):
            touched.append(ident)
    return bool(touched), touched


def check_code_drift(
    anchors: list[dict] | None,
    repo_root: str | Path | None = None,
) -> list[dict]:
    """Compare stored `code_anchors` against the current repository working tree.

    Returns a list of drift descriptors for anchors that have drifted:
      `[{"file": str, "reason": "modified"|"deleted", "old_sha": str, "new_sha": str|None, "identifiers": [...]}]`

    Note: If an anchored file had uncommitted changes when `code_anchors` were recorded
    (`old_sha` is not in the local git object database), hunk-level diffing against the
    prior blob is unavailable and any subsequent edit to that file conservatively reports
    drift (`reason='modified'`).
    """
    if not anchors or not isinstance(anchors, list):
        return []
    explicit_root = repo_root is not None
    root = Path(repo_root or os.getcwd()).resolve()
    drifted: list[dict] = []

    for a in anchors:
        if not isinstance(a, dict):
            continue
        rel = str(a.get("file") or "").strip()
        old_sha = str(a.get("blob_sha") or "").strip().lower()
        idents = [str(x) for x in (a.get("identifiers") or []) if x]
        if not rel or not old_sha:
            continue
        fpath = (root / rel).resolve()
        try:
            fpath.relative_to(root)
        except ValueError:
            continue

        if not fpath.exists():
            if not explicit_root:
                # When repo_root was not explicitly supplied, only mark deleted if git
                # history in CWD confirms this file belonged to this repository.
                try:
                    chk = subprocess.run(
                        ["git", "-C", str(root), "cat-file", "-e", old_sha],
                        capture_output=True, timeout=2, check=False,
                    )
                    if chk.returncode != 0:
                        continue
                except Exception:
                    continue
            drifted.append({
                "file": rel,
                "reason": "deleted",
                "old_sha": old_sha,
                "new_sha": None,
                "identifiers": idents,
            })
            continue

        new_sha = compute_git_blob_sha(fpath)
        if not new_sha or new_sha.startswith(old_sha) or old_sha.startswith(new_sha):
            continue

        if idents:
            try:
                cur_text = fpath.read_text(encoding="utf-8", errors="replace")
            except OSError:
                cur_text = ""
            is_drifted, touched = _git_diff_touches_identifiers(root, rel, old_sha, idents, cur_text)
            if not is_drifted:
                continue
            drifted.append({
                "file": rel,
                "reason": "modified",
                "old_sha": old_sha,
                "new_sha": new_sha,
                "identifiers": touched or idents,
            })
        else:
            drifted.append({
                "file": rel,
                "reason": "modified",
                "old_sha": old_sha,
                "new_sha": new_sha,
                "identifiers": [],
            })

    return drifted


def format_drift_notice(drifted: list[dict] | None) -> str:
    """Format a compact recall-time annotation for drifted code anchors."""
    if not drifted:
        return ""
    parts = []
    for d in drifted[:3]:
        f = d.get("file") or "file"
        reason = d.get("reason") or "modified"
        parts.append(f"{f} {reason} since recorded")
    return "code changed: " + "; ".join(parts)


def annotate_rows_with_code_drift(
    rows: list[dict],
    repo_root: str | Path | None = None,
) -> list[dict]:
    """Annotate recalled rows in-place when their `code_anchors` have drifted in `repo_root`."""
    if not rows:
        return rows
    for r in rows:
        anchors = r.get("code_anchors")
        if not anchors:
            continue
        drifted = check_code_drift(anchors, repo_root=repo_root)
        if drifted:
            r["code_drift"] = drifted
            r["code_drift_notice"] = format_drift_notice(drifted)
    return rows
