"""Regression guard for issue #67 (uv is primary; pip/pipx are documented fallbacks only):
fails if a bare, unlabeled `pip install` string shows up in user-facing docs or CLI source.

"Bare" = not itself `uv pip install ...`, and with no fallback/prohibition marker (e.g.
"fallback", "alternatively", "or:", "don't", "no uv") within the same line or an adjacent
line — the house style used throughout this repo (see README.md, QUICKSTART.md,
docs/integrations/omnigent.md, memnos_cli.py's server-setup omnigent messages, ...).

This does NOT scan `.github/workflows/*.yml` (internal CI tooling installing memnos's own
build/test deps — never shown to an end user, explicitly out of scope per issue #67) or
`tests/` (self-referential — this file and test_server_setup_omnigent.py legitimately
contain the string "pip install" inside their own assertions/comments).

Pure filesystem check — no DB, no server. Run: python tests/test_pip_install_guard.py
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PASS = FAIL = 0

# A bare "pip install" is one NOT immediately preceded by "uv " (i.e. not itself the
# uv-first form already-fixed lines use, `uv pip install ...`).
_PIP_INSTALL = re.compile(r"pip install", re.IGNORECASE)
_UV_PREFIX = re.compile(r"\buv\s*$", re.IGNORECASE)

# Any of these appearing within the same line, or the line immediately before/after,
# counts as the "explicit fallback qualifier" the issue asks for — covers this repo's
# established idioms ("(or: pip install ...)"), plain English framing ("fallback",
# "alternatively", "if you don't have uv"), and explicit prohibition ("don't pip install").
_MARKER = re.compile(
    r"fallback|alternativ|don'?t|do not|never|\bor:|works too|fine too|"
    r"no uv|without uv|instead of uv",
    re.IGNORECASE,
)

# Scope per issue #67's regression-guard ask: README/QUICKSTART/docs/**/*.md + CLI source.
# RELEASING.md included too — it's one of the issue's named "known occurrence" files and a
# root-level user-facing doc of the same kind. memnos_server.py is deliberately excluded:
# its one "pip install" mention (a docstring comment about setdefault() behavior) is not an
# install instruction shown to a user, so it isn't part of what this guard polices.
def _scanned_files():
    paths = []
    for name in ("README.md", "QUICKSTART.md", "RELEASING.md"):
        p = os.path.join(ROOT, name)
        if os.path.exists(p):
            paths.append(p)
    docs_dir = os.path.join(ROOT, "docs")
    for dirpath, _dirnames, filenames in os.walk(docs_dir):
        for fn in filenames:
            if fn.endswith(".md"):
                paths.append(os.path.join(dirpath, fn))
    cli_source = os.path.join(ROOT, "memnos_cli.py")
    if os.path.exists(cli_source):
        paths.append(cli_source)
    return sorted(paths)


def find_bare_pip_installs(text):
    """Return [(line_no, line_text), ...] for every 'pip install' in `text` that is
    neither itself `uv pip install ...` nor accompanied by a nearby fallback/prohibition
    marker. Empty list means the text is fully compliant."""
    violations = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        for m in _PIP_INSTALL.finditer(line):
            if _UV_PREFIX.search(line[:m.start()]):
                continue                                    # "uv pip install ..." — fine
            window = "\n".join(lines[max(0, i - 1):min(len(lines), i + 2)])
            if _MARKER.search(window):
                continue                                    # labeled fallback / prohibition
            violations.append((i + 1, line.strip()))
    return violations


def check(name, cond, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if (detail and not cond) else ""))
    PASS += bool(cond); FAIL += (not cond)


def main():
    print("=== self-test: the checker actually catches what it claims to (issue #67) ===")
    check("flags a genuinely bare, unlabeled pip install",
          find_bare_pip_installs("Get started:\npip install memnos-sdk\nThen run it.") != [])
    check("does NOT flag 'uv pip install ...' (already uv-first)",
          find_bare_pip_installs("uv pip install memnos-sdk") == [])
    check("does NOT flag a pip install labeled via '(or: ...)' on the same line",
          find_bare_pip_installs("uv pip install memnos\n# (or: pip install memnos)") == [])
    check("does NOT flag a pip install labeled via 'fallback' on the previous line",
          find_bare_pip_installs("Fallback if you lack uv:\npip install memnos") == [])
    check("does NOT flag a prohibition ('don't pip install ...')",
          find_bare_pip_installs("don't\n`pip install` into your system Python") == [])
    check("DOES flag a labeled marker that's too far away (not adjacent)",
          find_bare_pip_installs(
              "Fallback note, way up here.\n\n\n\npip install memnos-sdk") != [])

    print("=== scanning real repo files for unlabeled 'pip install' (issue #67) ===")
    scanned = _scanned_files()
    check(f"found files to scan ({len(scanned)})", len(scanned) > 0)
    total_violations = 0
    for path in scanned:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        violations = find_bare_pip_installs(text)
        rel = os.path.relpath(path, ROOT)
        detail = "; ".join(f"line {n}: {t[:80]!r}" for n, t in violations[:5])
        check(f"{rel}: no unlabeled 'pip install'", not violations, detail)
        total_violations += len(violations)
    check("zero unlabeled 'pip install' occurrences across all scanned files",
          total_violations == 0)

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
