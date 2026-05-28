#!/usr/bin/env python3
"""
engram-heartbeat.py — cross-platform background daemon (Mac / Linux / Windows).

Launched once per machine by the UserPromptSubmit hook. Uses a PID file so only
one instance ever runs. Every 10 minutes it scans all Claude Code transcript files
modified recently, generates a session summary via Claude Haiku, and writes it to
engram. This is the safety net for Ctrl+C, power loss, kill -9, and abrupt exits —
the transcript is always on disk even when the session dies, so this daemon catches
everything the in-process hooks miss.
"""

import json
import os
import pathlib
import platform
import signal
import sys
import time
import urllib.request

# ── Config (read from engram.env if present) ─────────────────────────────────
def load_env():
    env = {}
    env_path = pathlib.Path(__file__).parent / "engram.env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env

cfg = load_env()
ENGRAM_API = cfg.get("ENGRAM_API",        os.environ.get("ENGRAM_API",        "http://localhost:8766"))
ENGRAM_KEY = cfg.get("ENGRAM_KEY",        os.environ.get("ENGRAM_KEY",        ""))
DEFAULT_NS = cfg.get("ENGRAM_DEFAULT_NS", os.environ.get("ENGRAM_DEFAULT_NS", "personal:me"))
INTERVAL   = int(cfg.get("ENGRAM_HEARTBEAT_MINUTES", "10")) * 60

# ── PID file — one daemon per machine ────────────────────────────────────────
TMP = pathlib.Path(os.environ.get("TEMP", "/tmp"))
PID_FILE  = TMP / "engram_heartbeat.pid"
MARK_FILE = TMP / "engram_heartbeat_marker"

def already_running() -> bool:
    if not PID_FILE.exists():
        return False
    try:
        pid = int(PID_FILE.read_text().strip())
        if platform.system() == "Windows":
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        else:
            os.kill(pid, 0)   # signal 0 = just check existence
            return True
    except (ProcessLookupError, ValueError, OSError):
        return False

def write_pid():
    PID_FILE.write_text(str(os.getpid()))

def cleanup(*_):
    PID_FILE.unlink(missing_ok=True)
    sys.exit(0)

# ── Transcript discovery ──────────────────────────────────────────────────────
def find_active_transcripts(since_seconds: int = 900) -> list[pathlib.Path]:
    """Return .jsonl transcripts modified within the last `since_seconds`."""
    projects_dir = pathlib.Path.home() / ".claude" / "projects"
    if not projects_dir.exists():
        return []
    cutoff = time.time() - since_seconds
    return [
        p for p in projects_dir.rglob("*.jsonl")
        if p.stat().st_mtime > cutoff
    ]

# ── Extract recent turns from transcript ─────────────────────────────────────
def extract_turns(transcript: pathlib.Path, max_turns: int = 12) -> list[str]:
    turns = []
    try:
        for line in transcript.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                role = d.get("type", "")
                if role not in ("user", "assistant"):
                    continue
                msg = d.get("message", {})
                content = msg.get("content", "")
                text = ""
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text":
                            text += c.get("text", "")
                text = text.strip()
                if len(text) > 20:
                    turns.append(f"{role.upper()}: {text[:500]}")
            except Exception:
                continue
    except Exception:
        pass
    return turns[-max_turns:]

# ── Generate summary via claude --print (no API key needed) ──────────────────
def summarise(turns: list[str], project: str, branch: str) -> str:
    import shutil, subprocess
    if shutil.which("claude") is None or len(turns) < 2:
        return ""
    prompt = (
        f"Project: {project}" + (f"  branch: {branch}" if branch else "") +
        "\n\n[HEARTBEAT — session may have ended abruptly]\n\n" +
        "\n\n".join(turns) +
        '\n\nCapture this session for recovery. Write a dense, specific summary: '
        "what was being worked on, current status, any in-progress changes, last known state. "
        "Name tickets, files, functions. Be concise (max 180 words). "
        'End with "STATUS: <in-progress|blocked|complete|unknown>".\n'
        "IMPORTANT: respond with PLAIN TEXT ONLY. Do not generate any tool calls, "
        "<function_calls> XML, or <invoke> tags."
    )
    try:
        import re
        result = subprocess.run(
            ["claude", "--print", "--no-session-persistence", "--strict-mcp-config", "--tools", ""],
            input=prompt, capture_output=True, text=True, timeout=60
        )
        text = result.stdout
        text = re.sub(r"<function_calls>.*?</function_calls>", "", text, flags=re.DOTALL)
        text = re.sub(r"<tool_call>.*?</tool_call>", "", text, flags=re.DOTALL)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()[:800]
    except Exception:
        return ""

# ── Write memory to engram ────────────────────────────────────────────────────
def write_memory(content: str, namespace: str, project: str, session_id: str):
    if not ENGRAM_KEY:
        return
    payload = json.dumps({
        "content": content,
        "namespace": namespace,
        "memory_type": "session",
        "tags": ["session-summary", "heartbeat", "auto", project],
        "metadata": {"session_id": session_id, "project": project, "source": "heartbeat-daemon"},
        "provenance": {"tool": "engram-heartbeat-daemon", "agent_id": session_id},
    }).encode()
    req = urllib.request.Request(
        f"{ENGRAM_API}/api/v1/memory/",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {ENGRAM_KEY}",
            "X-Engram-Tool": "heartbeat-daemon",
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass

# ── Per-session last-save tracking ───────────────────────────────────────────
last_saved: dict[str, float] = {}

def process_transcript(transcript: pathlib.Path):
    session_id = transcript.stem
    now = time.time()

    # Skip if saved within the last 8 minutes (avoid duplicating the in-process periodic hook)
    if now - last_saved.get(session_id, 0) < 480:
        return

    # Derive project / cwd from path slug
    slug = transcript.parent.name          # e.g. -Users-foo-git-hdig-hdig-modules
    cwd  = slug.replace("-", "/", 1) if slug.startswith("-") else slug
    project = pathlib.Path(cwd).name or "unknown"
    branch  = ""
    try:
        import subprocess
        branch = subprocess.check_output(
            ["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
            stderr=subprocess.DEVNULL, text=True
        ).strip()
    except Exception:
        pass

    # Namespace
    ns = DEFAULT_NS
    engram_file = pathlib.Path(cwd) / ".engram"
    if engram_file.exists():
        for line in engram_file.read_text().splitlines():
            if line.startswith("namespace="):
                ns = line.split("=", 1)[1].strip()
                break

    turns = extract_turns(transcript)
    summary = summarise(turns, project, branch)
    if not summary:
        return

    content = f"[heartbeat] {project}" + (f" | {branch}" if branch else "") + f" — {summary}"
    write_memory(content, ns, project, session_id)
    last_saved[session_id] = now

# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    if already_running():
        sys.exit(0)

    # Detach from terminal on Unix so Ctrl+C in the parent doesn't kill this
    if platform.system() != "Windows":
        try:
            if os.fork() > 0:
                sys.exit(0)          # parent exits, child continues
        except AttributeError:
            pass                     # Windows — no fork, just run in background thread

    write_pid()
    signal.signal(signal.SIGTERM, cleanup)
    try:
        signal.signal(signal.SIGHUP, signal.SIG_IGN)   # survive terminal close on Unix
    except AttributeError:
        pass   # Windows has no SIGHUP

    MARK_FILE.touch()

    while True:
        try:
            transcripts = find_active_transcripts(since_seconds=INTERVAL + 300)
            for t in transcripts:
                try:
                    process_transcript(t)
                except Exception:
                    pass
        except Exception:
            pass
        time.sleep(INTERVAL)

if __name__ == "__main__":
    main()
