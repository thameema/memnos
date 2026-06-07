#!/usr/bin/env python3
"""memnos remember hook (Stop) — save the user's last message to memnos.
Fire-and-forget. Env: MEMNOS_URL, MEMNOS_NS, MEMNOS_TOKEN."""
import json, os, sys, urllib.request

URL = os.environ.get("MEMNOS_URL", "http://127.0.0.1:8900")
NS = os.environ.get("MEMNOS_NS", "claude:default")
TOKEN = os.environ.get("MEMNOS_TOKEN", "")          # server requires a Bearer token (write)
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
text = data.get("prompt", "")
tp = data.get("transcript_path", "")
if tp and os.path.exists(tp):
    try:
        last = ""
        with open(tp) as f:
            for line in f:
                ev = json.loads(line)
                c = ev.get("message", {}).get("content")
                if ev.get("type") == "user" and isinstance(c, str):
                    last = c
        text = last or text
    except Exception:
        pass
text = text.strip()
if not text:
    sys.exit(0)

# SKIP noise: agent/system-generated prompts and trivial turns pollute memory
# (e.g. autonomous-loop ticks, system reminders, one-word replies like "Yes").
_SKIP_PREFIX = ("# autonomous loop", "<system-reminder", "<command-", "# ")
_lower = text.lower()
if any(_lower.startswith(p) for p in _SKIP_PREFIX) or "<<autonomous-loop" in _lower:
    sys.exit(0)
if len(text) < 15 or len(text.split()) < 3:        # trivial-turn salience gate
    sys.exit(0)

try:
    headers = {"Content-Type": "application/json"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    req = urllib.request.Request(f"{URL}/remember", method="POST",
        data=json.dumps({"namespace": NS, "text": text, "speaker": "user"}).encode(),
        headers=headers)
    urllib.request.urlopen(req, timeout=12).read()
except Exception:
    pass
