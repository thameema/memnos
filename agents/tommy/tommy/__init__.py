"""Tommy — personal coding orchestrator built on memnos."""
from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    # Single source of truth: the version actually recorded in the installed
    # distribution's metadata (which pip/uv set from pyproject.toml at build
    # time). Never hardcode a version literal anywhere else in this package —
    # read tommy.__version__ instead, so there is nothing left to drift.
    __version__ = _pkg_version("tommy-orchestrator")
except PackageNotFoundError:
    # Running from a source checkout with no installed distribution
    # (e.g. `python -m tommy.cli` against a raw clone).
    __version__ = "0.0.0-dev"
