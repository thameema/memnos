#!/usr/bin/env python3
"""memnos remember hook (Stop) — save the user's last message to memnos.
Fire-and-forget. Env: MEMNOS_URL, MEMNOS_NS."""
import json, os, sys, urllib.request

URL = os.environ.get("MEMNOS_URL", "http://127.0.0.1:8900")
NS = os.environ.get("MEMNOS_NS", "claude:default")
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
if not text.strip():
    sys.exit(0)
try:
    req = urllib.request.Request(f"{URL}/remember", method="POST",
        data=json.dumps({"namespace": NS, "text": text, "speaker": "user"}).encode(),
        headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=12).read()
except Exception:
    pass
