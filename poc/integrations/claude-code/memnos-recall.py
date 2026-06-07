#!/usr/bin/env python3
"""memnos recall hook (UserPromptSubmit) — inject relevant memories before Claude
answers. No LLM at query time (vector + rerank). Env: MEMNOS_URL, MEMNOS_NS, MEMNOS_TOKEN."""
import json, os, sys, urllib.request

URL = os.environ.get("MEMNOS_URL", "http://127.0.0.1:8900")
NS = os.environ.get("MEMNOS_NS", "claude:default")
TOKEN = os.environ.get("MEMNOS_TOKEN", "")          # server requires a Bearer token
try:
    prompt = json.load(sys.stdin).get("prompt", "")
except Exception:
    sys.exit(0)
if not prompt.strip():
    sys.exit(0)
try:
    headers = {"Content-Type": "application/json"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    req = urllib.request.Request(f"{URL}/recall", method="POST",
        data=json.dumps({"namespace": NS, "query": prompt}).encode(),
        headers=headers)
    ctx = json.load(urllib.request.urlopen(req, timeout=12)).get("context", "")
except Exception:
    sys.exit(0)                      # fail open — never block the prompt
if ctx.strip():
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": "## Relevant memories (memnos)\n" + ctx}}))
