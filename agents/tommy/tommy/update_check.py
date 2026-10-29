"""Tommy update check: once-a-day PyPI check, a print-once-per-version notice, and an
opt-in auto-update (default OFF) — the same shape as memnos_gateway.py's update-check
loop, adapted to Tommy's lifecycle.

Why this can't just reuse memnos_gateway.py's loop: that one lives in a long-running
daemon (the memnos gateway), so a background thread that sleeps a day between ticks has
somewhere to live. Tommy has no daemon — the interactive `tommy` CLI launches a harness
and exits when it exits; `tommy --mcp` is a per-editor-session stdio process with no
persistent background thread of its own. So instead of a sleep-loop, this is a
check-on-launch: cheap (one local JSON read) on every launch, and only touches the
network when the cache says it's been >= CHECK_INTERVAL_S since the last successful
check. Deliberately duplicates memnos_gateway.py's _latest_pypi_version/_vparts rather
than importing it — tommy-orchestrator is published and installed separately from the
memnos server package and must not depend on it at runtime (see docs/guides/tommy.md's
Installation section).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

CACHE_PATH = Path.home() / ".memnos" / "agents" / "tommy" / "update_check.json"
PACKAGE = "tommy-orchestrator"
CHECK_ENABLED = os.environ.get("TOMMY_UPDATE_CHECK", "1") != "0"
CHECK_INTERVAL_S = float(os.environ.get("TOMMY_UPDATE_CHECK_INTERVAL_S", str(24 * 3600)))


def _vparts(v: Optional[str]) -> tuple:
    out = []
    for p in (v or "0").split(".")[:4]:
        n = "".join(c for c in p if c.isdigit())
        out.append(int(n) if n else 0)
    return tuple(out)


def _latest_pypi_version(timeout: float = 2.0) -> Optional[str]:
    import urllib.request
    try:
        with urllib.request.urlopen(f"https://pypi.org/pypi/{PACKAGE}/json", timeout=timeout) as r:
            return json.load(r)["info"]["version"]
    except Exception:
        return None


def _read_state() -> dict:
    try:
        with open(CACHE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _write_state(**updates) -> None:
    """Read-modify-write so an unrelated field (e.g. `auto_update`, set by a concurrent
    `tommy --upgrade --auto-update`) survives a check-tick's own write, and vice versa."""
    state = _read_state()
    state.update(updates)
    os.makedirs(CACHE_PATH.parent, exist_ok=True)
    tmp = CACHE_PATH.with_suffix(f".tmp{os.getpid()}")
    try:
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, CACHE_PATH)
    except Exception:
        pass  # a torn write here just means this tick's cache update is lost, not fatal


def read_auto_update_pref() -> bool:
    return bool(_read_state().get("auto_update", False))


def set_auto_update(enabled: bool) -> None:
    _write_state(auto_update=enabled)


def _spawn_tommy_upgrade() -> None:
    """Launch `tommy --upgrade` as a separate, detached process — never run the
    installer from inside this already-loaded process. Resolves the `tommy` binary next
    to this process's own sys.executable (not a bare PATH lookup), same reasoning as
    memnos_gateway.py's _spawn_auto_update: a minimal-PATH environment (e.g. launched
    from an editor) may not have the uv-tool/pipx install dir on PATH."""
    tommy_bin = os.path.join(os.path.dirname(sys.executable), "tommy")
    if not os.path.exists(tommy_bin):
        tommy_bin = "tommy"
    log_path = CACHE_PATH.parent / "auto_update.log"
    logf = None
    try:
        os.makedirs(log_path.parent, exist_ok=True)
        logf = open(log_path, "a")
        subprocess.Popen([tommy_bin, "--upgrade"], stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
    except Exception as e:
        print(f"[tommy] AUTO-UPDATE: failed to launch {tommy_bin}: {type(e).__name__}: {e}", file=sys.stderr)
    finally:
        if logf is not None:
            logf.close()


def check_and_maybe_notify(current_version: Optional[str]) -> None:
    """One check-on-launch tick. Never raises — called from a background thread off the
    interactive launch path, and a failure here must never be allowed to look like a
    Tommy crash. Only prints the "update available" line once per newly-seen version
    (tracked via `last_notified` in the cache) — unlike memnos_gateway's daily tick,
    `tommy` can be launched many times a day, and re-printing the same notice on every
    single launch would be pure noise."""
    if not CHECK_ENABLED or not current_version:
        return
    try:
        state = _read_state()
        stale = (time.time() - state.get("checked_at", 0)) >= CHECK_INTERVAL_S
        latest = state.get("latest_known") if not stale else None
        if stale:
            fetched = _latest_pypi_version()
            if fetched:
                latest = fetched
                _write_state(latest_known=fetched, checked_at=time.time())
            # fetch failed: fall back to whatever was last cached (may be stale or None);
            # checked_at is NOT advanced, so the next launch retries rather than waiting
            # out the full interval again.
            elif latest is None:
                latest = state.get("latest_known")
        if not latest or _vparts(latest) <= _vparts(current_version):
            return
        if latest != state.get("last_notified"):
            print(f"[tommy] UPDATE: v{current_version} -> v{latest} available "
                  f"(https://github.com/thameema/memnos/releases/tag/tommy-v{latest}) — "
                  "run `tommy --upgrade`, or `tommy --upgrade --auto-update` to stop seeing this.",
                  file=sys.stderr)
            _write_state(last_notified=latest)
        if read_auto_update_pref():
            print(f"[tommy] AUTO-UPDATE: v{current_version} -> v{latest}, running tommy --upgrade", file=sys.stderr)
            _spawn_tommy_upgrade()
    except Exception:
        pass


def check_in_background(current_version: Optional[str]) -> None:
    """Fire-and-forget: start the check on a daemon thread so it never adds latency to
    the interactive launch path. Daemon=True so it never blocks process exit even if
    still waiting on a slow/unreachable PyPI when the harness session ends."""
    if not CHECK_ENABLED or not current_version:
        return
    threading.Thread(target=check_and_maybe_notify, args=(current_version,), daemon=True).start()
