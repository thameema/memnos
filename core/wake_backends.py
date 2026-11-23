"""Pluggable backends for `memnos wake-daemon` (Layer C of the memnos-wake design).

memnos core never shells out to a terminal multiplexer directly -- it only calls
WakeBackend.is_idle()/send_doorbell(). Orca and tmux ship as backends here because
that's what's actually in use, but neither is required: with nothing installed,
select_backend() returns NoopBackend and idle-wake is simply disabled (sessions still
get messages on their next prompt via the existing pull path).

Security invariant: the doorbell text is always the static template from
format_doorbell() -- an id and a session name, never raw peer message content pasted
into a shell command.
"""
import json
import os
import shutil
import subprocess
import time


class WakeBackend:
    def is_idle(self, session_metadata: dict):
        """True/False, or None if it can't be determined (-> daemon does not wake it)."""
        raise NotImplementedError

    def send_doorbell(self, session_metadata: dict, text: str) -> bool:
        """Deliver `text` to the session's terminal. Returns True if delivered."""
        raise NotImplementedError

    @staticmethod
    def available() -> bool:
        return False


class OrcaBackend(WakeBackend):
    def __init__(self, orca_bin="orca"):
        self.orca_bin = shutil.which(orca_bin) or ("/usr/local/bin/orca" if os.path.isfile("/usr/local/bin/orca") else orca_bin)

    def _list_terminals(self):
        try:
            out = subprocess.run([self.orca_bin, "terminal", "list", "--json"],
                                  capture_output=True, text=True, timeout=5, check=True)
            parsed = json.loads(out.stdout or "{}")
            if isinstance(parsed, list):
                return parsed
            return ((parsed.get("result") or {}).get("terminals")) or []
        except Exception:
            return []

    def _match_terminal(self, session_metadata):
        terms = self._list_terminals()
        term = session_metadata.get("orca_terminal_id")
        if term:
            for t in terms:
                if t.get("handle") == term or t.get("id") == term or t.get("terminal_id") == term:
                    return t
        cwd = session_metadata.get("cwd") or ""
        harness = session_metadata.get("harness") or ""
        expected = {"claude-code": "claude", "claude": "claude", "antigravity": "antigravity", "agy": "antigravity"}.get(harness, harness)
        matches = [
            t for t in terms
            if cwd and t.get("worktreePath") == cwd and expected and t.get("agentIdentity") == expected
        ]
        return matches[-1] if matches else None

    def is_idle(self, session_metadata):
        t = self._match_terminal(session_metadata)
        if not t:
            return None
        if not t.get("connected", True) or not t.get("writable", True):
            return False
        st = t.get("state") or t.get("status")
        if st:
            return st in ("waiting", "tui-idle", "idle")
        last_out = t.get("lastOutputAt") or 0
        if last_out and (time.time() * 1000.0 - float(last_out)) < 4500:
            return False
        preview = t.get("preview") or ""
        tail = "\n".join(preview.splitlines()[-3:])
        low = tail.lower()
        if "running" in low or "esc to interrupt" in low or "esc to cancel" in low:
            return False
        if any(ch in tail for ch in "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏⣽⣾⣷⣯⣟⡿⢿⣻"):
            return False
        return True

    def send_doorbell(self, session_metadata, text):
        t = self._match_terminal(session_metadata)
        handle = (t or {}).get("handle") or (t or {}).get("id") or session_metadata.get("orca_terminal_id")
        if not handle:
            return False
        try:
            subprocess.run([self.orca_bin, "terminal", "send", "--terminal", handle,
                             "--text", text, "--enter", "--wait-submit", "5", "--json"],
                            capture_output=True, text=True, timeout=10, check=True)
            return True
        except Exception:
            return False

    @staticmethod
    def available():
        return shutil.which("orca") is not None


class TmuxBackend(WakeBackend):
    """Best-effort: tmux can't distinguish "idle at prompt" from "a long-running
    foreground command" as precisely as orca's terminal state does.
    shortcut: treats "shell is the foreground process, not in copy-mode" as idle;
    upgrade to a richer heuristic (e.g. shell-integration hooks) if this misfires."""

    def is_idle(self, session_metadata):
        pane = session_metadata.get("tmux_pane")
        if not pane:
            return None
        try:
            out = subprocess.run(
                ["tmux", "list-panes", "-a", "-F", "#{pane_id} #{pane_in_mode} #{pane_current_command}"],
                capture_output=True, text=True, timeout=5, check=True)
        except Exception:
            return None
        for line in out.stdout.splitlines():
            parts = line.split()
            if not parts or parts[0] != pane:
                continue
            in_mode = len(parts) > 1 and parts[1] == "1"
            cmd = parts[2] if len(parts) > 2 else ""
            return (not in_mode) and cmd in ("bash", "zsh", "sh", "fish")
        return None

    def send_doorbell(self, session_metadata, text):
        pane = session_metadata.get("tmux_pane")
        if not pane:
            return False
        try:
            subprocess.run(["tmux", "send-keys", "-t", pane, text, "Enter"],
                            capture_output=True, text=True, timeout=10, check=True)
            return True
        except Exception:
            return False

    @staticmethod
    def available():
        return bool(os.environ.get("TMUX")) or shutil.which("tmux") is not None


class NoopBackend(WakeBackend):
    _warned = set()

    def is_idle(self, session_metadata):
        name = session_metadata.get("name", "?")
        if name not in NoopBackend._warned:
            NoopBackend._warned.add(name)
            print(f"[memnos-wake] no wake backend available for session {name} "
                  f"-- idle wake disabled, falls back to pull on next prompt", flush=True)
        return None

    def send_doorbell(self, session_metadata, text):
        return False

    @staticmethod
    def available():
        return True


_BACKENDS = {"orca": OrcaBackend, "tmux": TmuxBackend, "noop": NoopBackend}


def select_backend(name=None):
    name = (name or "").strip().lower()
    if name:
        cls = _BACKENDS.get(name)
        if cls is None:
            raise ValueError(f"unknown wake backend '{name}' (choices: {', '.join(_BACKENDS)})")
        return cls()
    for cls in (OrcaBackend, TmuxBackend, NoopBackend):
        if cls.available():
            return cls()
    return NoopBackend()


def format_doorbell(session_name: str, message_id) -> str:
    """The ONLY string ever sent as a doorbell -- never raw peer message content."""
    return f"New Memnos message (#{message_id}) for @{session_name} — poll and process via memnos MCP."
