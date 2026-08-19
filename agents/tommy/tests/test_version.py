"""
Regression tests for Tommy's version reporting.

Root cause (real user report, 2026-08): the CLI launch banner hardcoded a
literal "v0.1.0" string in cli.py, completely independent of the version
tommy-orchestrator was actually built/published at (pyproject.toml, PyPI).
A user upgraded via `uv tool upgrade` on another machine — pip/uv correctly
reported the new version — but the banner still printed v0.1.0, which reads
exactly like a failed upgrade. Compounding it: `tommy --version` was not a
registered click option at all. With
context_settings={"ignore_unknown_options": True, "allow_extra_args": True}
and an UNPROCESSED extra_args catch-all, "--version" silently fell into
extra_args and `main()` proceeded to print the banner and attempt a full
launch (memnos health check, harness spawn) instead of printing a version
and exiting.

Fix: tommy.__version__ (agents/tommy/tommy/__init__.py) now reads
importlib.metadata.version("tommy-orchestrator") once — the single source
of truth, sourced from the installed distribution's metadata, which pip/uv
set from pyproject.toml at install/build time. Everywhere else in the
package (banner, the new --version flag, the MCP clientInfo handshake in
discovery/mcp.py) imports that one value instead of hardcoding a literal.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import tommy
from tommy.cli import _print_banner, TOMMY_LOGO
from tommy.config import TommyConfig

_PACKAGE_DIR = Path(tommy.__file__).parent
_TOMMY_ROOT = _PACKAGE_DIR.parent  # agents/tommy/


def _all_source_files() -> list[Path]:
    return sorted(_PACKAGE_DIR.rglob("*.py"))


# A quoted string literal whose ENTIRE content is X.Y.Z digits — e.g. "0.1.0"
# or '0.1.2'. Anchored to the quote chars (via backreference) so it does NOT
# match legitimate non-version literals that merely *contain* a dotted-digit
# substring, e.g. "0.0.0-dev" (trailing "-dev" breaks the match) or
# "127.0.0.1" / "http://127.0.0.1:8900" (four dot-groups, or extra
# scheme/port characters, break the match).
_SEMVER_LITERAL = re.compile(r"""(['"])(\d+\.\d+\.\d+)\1""")


class TestNoHardcodedVersionLiteral:
    """Structural guard: no quoted X.Y.Z semver literal anywhere in tommy/."""

    def test_no_quoted_semver_literal_in_package_source(self):
        offenders = []
        for path in _all_source_files():
            src = path.read_text()
            for m in _SEMVER_LITERAL.finditer(src):
                rel = path.relative_to(_TOMMY_ROOT)
                line_no = src.count("\n", 0, m.start()) + 1
                offenders.append(f"{rel}:{line_no}: {m.group(0)}")
        assert offenders == [], (
            "Found hardcoded semver-looking string literal(s) in the tommy "
            "package. Version strings must always come from tommy.__version__ "
            "(importlib.metadata) — never a literal. A literal is exactly how "
            "the launch banner drifted to a stale v0.1.0 while pyproject.toml "
            "and PyPI moved on to 0.1.2. Offenders:\n" + "\n".join(offenders)
        )


class TestDynamicVersion:
    def test_package_version_is_not_the_dev_fallback(self):
        # Sanity: this suite must run against an installed tommy-orchestrator
        # (editable or wheel) so importlib.metadata resolves a real version
        # rather than silently falling back to the source-checkout default.
        assert tommy.__version__ != "0.0.0-dev", (
            "tommy-orchestrator isn't installed in this environment. "
            "Run `pip install -e agents/tommy` before running this suite."
        )

    def test_package_version_matches_pyproject_declared_version(self):
        pyproject = _TOMMY_ROOT / "pyproject.toml"
        text = pyproject.read_text()
        m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
        assert m, 'could not find version = "..." in agents/tommy/pyproject.toml'
        assert tommy.__version__ == m.group(1), (
            f"tommy.__version__ ({tommy.__version__!r}) does not match the "
            f"version declared in pyproject.toml ({m.group(1)!r}). If this "
            "environment's installed tommy-orchestrator is stale, reinstall "
            "with `pip install -e agents/tommy`."
        )

    def test_banner_reports_dynamic_version(self, capsys):
        _print_banner(TommyConfig(), project_key=None)
        captured = capsys.readouterr()
        assert f"v{tommy.__version__}" in captured.err, (
            f"launch banner did not print v{tommy.__version__}; got:\n{captured.err}"
        )

    def test_version_flag_exits_immediately_without_launching(self):
        """
        Out-of-process, with a hard timeout: before this fix, an unrecognized
        --version fell through to a real launch attempt (banner + memnos
        health check + harness spawn). Running in-process here would risk a
        regression hanging the whole test suite instead of failing it; a
        subprocess with a timeout fails loudly and fast either way.
        """
        proc = subprocess.run(
            [sys.executable, "-c", "from tommy.cli import main; main()", "--version"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        assert tommy.__version__ in proc.stdout, proc.stdout
        # The critical regression signal: a real launch attempt prints the
        # banner text and/or the logo to stderr. --version must short-circuit
        # before any of that.
        assert "memnos-native coding orchestrator" not in proc.stdout
        assert "memnos-native coding orchestrator" not in proc.stderr
        assert TOMMY_LOGO.strip().splitlines()[0] not in proc.stderr

    def test_mcp_client_info_uses_dynamic_version(self):
        src = (_PACKAGE_DIR / "discovery" / "mcp.py").read_text()
        assert '"version": __version__' in src or "'version': __version__" in src, (
            "discovery/mcp.py's MCP clientInfo handshake should report "
            "tommy.__version__, not a hardcoded literal."
        )
