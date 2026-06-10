"""memnos proxy — LLM-API capture tee.

Point any OpenAI- or Anthropic-compatible client's base URL at this proxy
(`ANTHROPIC_BASE_URL` / `OPENAI_BASE_URL` → http://127.0.0.1:8910) and every completed,
user-facing exchange — the user's message AND the model's reply — is captured into memnos
automatically. Deterministic capture for any tool that allows a base-URL override; the
permanent fix for "only the question got remembered".

Design (see docs/guides/proxy.md):
- TRANSPARENT passthrough, same wire format in and out — no translation, no retries, no
  routing. Streaming SSE bytes are relayed to the client FIRST, then parsed off the hot
  path. Added latency is one local hop.
- FAIL-OPEN: any capture-side failure still relays the response untouched.
- BYOK: Authorization / x-api-key headers are forwarded verbatim and never stored/logged.
- Capture policy gates out agent-loop noise (tool-call iterations, title/summary calls):
  only terminal responses (stop/end_turn, no tool calls) to a human-text user message are
  kept, small/background models are denylisted, and an LRU dedupes resent turns.
- Writes go through the normal authenticated /remember API as the `proxy` principal —
  redacted, namespace-scoped, audited like every other write.
"""
import hashlib
import json
import os
import queue
import re
import sys
import threading
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".memnos", "config.json")
MAX_BODY = 20 * 1024 * 1024            # sanity cap — agent requests carry whole histories

DEFAULTS = {
    "port": 8910,
    "upstreams": {"openai": "https://api.openai.com", "anthropic": "https://api.anthropic.com"},
    "capture": True,
    "capture_model_denylist": ["*haiku*"],   # Claude Code uses haiku for titles/topic detection
    "capture_min_max_tokens": 512,           # tiny-max_tokens calls are title/summary chatter
}

# hop-by-hop / recomputed headers we must not forward
_SKIP_REQ = {"host", "content-length", "connection", "accept-encoding", "x-memnos-namespace"}
_SKIP_RESP = {"content-length", "connection", "transfer-encoding", "content-encoding"}


def _load_cfg():
    cfg = dict(DEFAULTS)
    try:
        c = json.load(open(CONFIG_PATH))
        cfg["server_url"] = f"http://127.0.0.1:{c.get('port', 8900)}"
        cfg["token"] = c.get("proxy_token") or c.get("admin_token", "")
        cfg["namespace"] = c.get("proxy_namespace") or f"user:{(os.environ.get('USER') or 'me').split()[0]}"
        p = c.get("proxy") or {}
        for k in DEFAULTS:
            if k in p:
                cfg[k] = p[k]
    except Exception:
        cfg.setdefault("server_url", "http://127.0.0.1:8900")
        cfg.setdefault("token", "")
        cfg.setdefault("namespace", f"user:{(os.environ.get('USER') or 'me').split()[0]}")
    if os.environ.get("MEMNOS_PROXY_PORT"):
        cfg["port"] = int(os.environ["MEMNOS_PROXY_PORT"])
    if os.environ.get("MEMNOS_URL"):
        cfg["server_url"] = os.environ["MEMNOS_URL"]
    if os.environ.get("MEMNOS_TOKEN"):
        cfg["token"] = os.environ["MEMNOS_TOKEN"]
    if os.environ.get("MEMNOS_NS"):
        cfg["namespace"] = os.environ["MEMNOS_NS"]
    return cfg


CFG = _load_cfg()


# ---- capture policy (pure functions — unit-tested without sockets) -----------------
def _model_denied(model):
    model = (model or "").lower()
    for pat in CFG["capture_model_denylist"]:
        rx = re.escape(pat.lower()).replace(r"\*", ".*")
        if re.fullmatch(rx, model):
            return True
    return False


def _block_text(content):
    """Text from a message content that may be a string or a block list. Returns None if
    any non-text block is present (tool_result/tool_use/image → not a human text turn)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for b in content:
            if not isinstance(b, dict) or b.get("type") != "text":
                return None
            out.append(b.get("text", ""))
        return "\n".join(out)
    return None


def _trailing_user_text(req):
    """The last message must be a human TEXT user turn — else this is an agent-loop
    iteration (tool_result follow-up) and is not captured."""
    msgs = req.get("messages") or []
    if not msgs:
        return None
    last = msgs[-1]
    if last.get("role") != "user":
        return None
    return _block_text(last.get("content"))


def _conv_key(req, fmt):
    sysp = req.get("system", "") if fmt == "anthropic" else ""
    if fmt == "openai":
        for m in req.get("messages") or []:
            if m.get("role") in ("system", "developer"):
                sysp = str(m.get("content", "")); break
    first = ""
    for m in req.get("messages") or []:
        if m.get("role") == "user":
            t = _block_text(m.get("content"))
            first = t or json.dumps(m.get("content"), default=str)[:500]
            break
    if isinstance(sysp, list):
        sysp = json.dumps(sysp, default=str)[:2000]
    return hashlib.sha256((str(sysp) + "\x00" + first).encode()).hexdigest()[:16]


def parse_response(body_bytes, fmt, streamed):
    """Summarize a (possibly SSE) response: {text, terminal, has_tools}. Never raises."""
    try:
        if streamed:
            return (_acc_openai_sse if fmt == "openai" else _acc_anthropic_sse)(body_bytes)
        d = json.loads(body_bytes)
        if fmt == "openai":
            ch = (d.get("choices") or [{}])[0]
            msg = ch.get("message") or {}
            return {"text": msg.get("content") or "",
                    "terminal": ch.get("finish_reason") == "stop",
                    "has_tools": bool(msg.get("tool_calls"))}
        text, has_tools = [], False
        for b in d.get("content") or []:
            if b.get("type") == "text":
                text.append(b.get("text", ""))
            elif b.get("type") == "tool_use":
                has_tools = True
        return {"text": "\n".join(text), "terminal": d.get("stop_reason") == "end_turn",
                "has_tools": has_tools}
    except Exception:
        return {"text": "", "terminal": False, "has_tools": False}


def _sse_events(body_bytes):
    for raw in body_bytes.decode("utf-8", "replace").split("\n"):
        raw = raw.strip()
        if raw.startswith("data:"):
            data = raw[5:].strip()
            if data and data != "[DONE]":
                try:
                    yield json.loads(data)
                except Exception:
                    pass


def _acc_openai_sse(body_bytes):
    text, finish, has_tools = [], None, False
    for ev in _sse_events(body_bytes):
        for ch in ev.get("choices") or []:
            delta = ch.get("delta") or {}
            if delta.get("content"):
                text.append(delta["content"])
            if delta.get("tool_calls"):
                has_tools = True
            if ch.get("finish_reason"):
                finish = ch["finish_reason"]
    return {"text": "".join(text), "terminal": finish == "stop", "has_tools": has_tools}


def _acc_anthropic_sse(body_bytes):
    text, stop, has_tools = [], None, False
    for ev in _sse_events(body_bytes):
        t = ev.get("type")
        if t == "content_block_delta" and (ev.get("delta") or {}).get("type") == "text_delta":
            text.append(ev["delta"].get("text", ""))
        elif t == "content_block_start" and (ev.get("content_block") or {}).get("type") == "tool_use":
            has_tools = True
        elif t == "message_delta":
            stop = (ev.get("delta") or {}).get("stop_reason") or stop
    return {"text": "".join(text), "terminal": stop == "end_turn", "has_tools": has_tools}


def should_capture(req, resp_summary):
    """Gate chain — returns (bool, reason). All gates must pass."""
    if not CFG["capture"]:
        return False, "capture disabled"
    if _model_denied(req.get("model", "")):
        return False, "model denylisted (background/title call)"
    mt = req.get("max_tokens") or req.get("max_completion_tokens")
    if isinstance(mt, int) and mt < CFG["capture_min_max_tokens"]:
        return False, f"max_tokens {mt} < {CFG['capture_min_max_tokens']} (background call)"
    if not resp_summary["terminal"] or resp_summary["has_tools"]:
        return False, "non-terminal response (tool-call loop iteration)"
    user_text = _trailing_user_text(req)
    if user_text is None:
        return False, "last request message is not a human text turn"
    user_text = user_text.strip()
    if len(user_text) < 15 or len(user_text.split()) < 3:
        return False, "user text too short"
    if len(resp_summary["text"].strip()) < 30:
        return False, "assistant text too short"
    return True, "ok"


# ---- capture worker (async — never on the relay hot path) --------------------------
class _LRU:
    def __init__(self, cap=4096):
        self._d, self._cap, self._lock = OrderedDict(), cap, threading.Lock()

    def seen(self, key):
        """True if already present; inserts otherwise."""
        with self._lock:
            if key in self._d:
                self._d.move_to_end(key)
                return True
            self._d[key] = True
            if len(self._d) > self._cap:
                self._d.popitem(last=False)
            return False


class CaptureWorker:
    def __init__(self):
        self.q = queue.Queue(maxsize=256)
        self.lru = _LRU()
        self.stats = {"captured": 0, "skipped": 0, "errors": 0, "relay_errors": 0, "last_error": None}
        threading.Thread(target=self._run, name="memnos-proxy-capture", daemon=True).start()

    def submit(self, req, resp_summary, fmt, namespace):
        try:
            self.q.put_nowait((req, resp_summary, fmt, namespace))
        except queue.Full:
            self.stats["errors"] += 1

    def _remember(self, text, speaker, namespace, session_id):
        h = hashlib.sha256(f"{namespace}|{speaker}|{' '.join(text.split())}".encode()).hexdigest()
        if self.lru.seen(h):
            return
        r = httpx.post(f"{CFG['server_url']}/remember",
                       json={"namespace": namespace, "text": text[:8000],
                             "speaker": speaker, "session_id": session_id},
                       headers={"Authorization": f"Bearer {CFG['token']}"}, timeout=20)
        r.raise_for_status()
        self.stats["captured"] += 1

    def _run(self):
        while True:
            req, summary, fmt, ns = self.q.get()
            try:
                ok, reason = should_capture(req, summary)
                if not ok:
                    self.stats["skipped"] += 1
                    continue
                sid = _conv_key(req, fmt)
                self._remember(_trailing_user_text(req).strip(), "user", ns, sid)
                self._remember(summary["text"].strip(), "assistant", ns, sid)
            except Exception as e:
                self.stats["errors"] += 1
                self.stats["last_error"] = f"capture: {type(e).__name__}: {e}"
                print(f"[memnos-proxy] capture error (relay unaffected): {type(e).__name__}: {e}",
                      flush=True)


WORKER = None


# ---- the proxy --------------------------------------------------------------------
class ProxyHandler(BaseHTTPRequestHandler):
    server_version = "memnos-proxy/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/healthz":
            body = json.dumps({"ok": True, "capture": CFG["capture"],
                               "stats": WORKER.stats if WORKER else {}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers()

    def do_POST(self):
        if self.path.startswith("/v1/messages"):
            fmt = "anthropic"
        elif self.path.startswith("/v1/chat/completions"):
            fmt = "openai"
        else:                                   # unknown path → relay-only to openai upstream
            fmt = "openai" if "/chat/" in self.path or self.path.startswith("/v1/") else None
        if fmt is None:
            self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers()
            return
        n = int(self.headers.get("Content-Length", 0))
        if n > MAX_BODY:
            self.send_response(413); self.send_header("Content-Length", "0"); self.end_headers()
            return
        body = self.rfile.read(n)
        ns = self.headers.get("x-memnos-namespace") or CFG["namespace"]
        fwd = {k: v for k, v in self.headers.items() if k.lower() not in _SKIP_REQ}
        upstream = CFG["upstreams"]["anthropic" if fmt == "anthropic" else "openai"].rstrip("/")
        url = upstream + self.path

        try:
            req_json = json.loads(body)
        except Exception:
            req_json = {}
        streamed = bool(req_json.get("stream"))
        capture_eligible = self.path.startswith(("/v1/messages", "/v1/chat/completions"))

        buf = bytearray()
        try:
            with httpx.stream("POST", url, content=body, headers=fwd,
                              timeout=httpx.Timeout(600, connect=15)) as r:
                self.send_response(r.status_code)
                for k, v in r.headers.items():
                    if k.lower() not in _SKIP_RESP:
                        self.send_header(k, v)
                # we re-frame the body ourselves (chunked for streams, length otherwise)
                if streamed:
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    for chunk in r.iter_raw():
                        if not chunk:
                            continue
                        self.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
                        self.wfile.flush()       # relay FIRST — capture parses later
                        buf += chunk
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                else:
                    for chunk in r.iter_raw():
                        buf += chunk
                    self.send_header("Content-Length", str(len(buf)))
                    self.end_headers()
                    self.wfile.write(bytes(buf))
                status = r.status_code
        except (BrokenPipeError, ConnectionResetError):
            return                               # client gave up mid-stream — drop capture
        except Exception as e:
            # unambiguous error taxonomy: the caller must always know WHO failed.
            # Provider errors (auth, rate limit, model) are relayed verbatim above with the
            # provider's own status/body — anything HERE is proxy↔upstream plumbing.
            if isinstance(e, httpx.ConnectTimeout):
                etype, msg = "upstream_connect_timeout", \
                    f"network: no answer from {upstream} within 15s (firewall/DNS/offline?)"
            elif isinstance(e, httpx.ConnectError):
                etype, msg = "upstream_unreachable", \
                    f"network: could not connect to {upstream} ({e})"
            elif isinstance(e, httpx.ReadTimeout):
                etype, msg = "upstream_read_timeout", \
                    f"network: {upstream} accepted the request but stopped responding (read timeout 600s)"
            else:
                etype, msg = "proxy_error", f"memnos-proxy internal error: {type(e).__name__}: {e}"
            if WORKER is not None:
                WORKER.stats["relay_errors"] = WORKER.stats.get("relay_errors", 0) + 1
                WORKER.stats["last_error"] = f"{etype}: {msg}"
            print(f"[memnos-proxy] {etype}: {msg}", flush=True)
            err = json.dumps({"error": {"type": etype, "source": "memnos-proxy",
                                        "message": msg + " — this is NOT an error from the "
                                        "LLM provider; check the proxy/network, or bypass the "
                                        "proxy by restoring the original base URL"}}).encode()
            try:
                self.send_response(502 if etype != "proxy_error" else 500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(err)))
                self.end_headers()
                self.wfile.write(err)
            except Exception:
                pass
            return

        # capture AFTER the client has the full response — never on the hot path
        try:
            if capture_eligible and 200 <= status < 300 and WORKER is not None:
                WORKER.submit(req_json, parse_response(bytes(buf), fmt, streamed), fmt, ns)
        except Exception:
            pass


def serve(port=None):
    global WORKER
    port = int(port or CFG["port"])
    WORKER = CaptureWorker()
    print(f"[memnos-proxy] capture proxy on http://127.0.0.1:{port}", flush=True)
    print(f"  upstreams: openai → {CFG['upstreams']['openai']}   anthropic → {CFG['upstreams']['anthropic']}", flush=True)
    print(f"  capture:   {'ON  → namespace ' + CFG['namespace'] if CFG['capture'] else 'OFF (relay only)'}", flush=True)
    print("  keys:      forwarded verbatim to the upstream — never stored, never logged", flush=True)
    print(f"  point clients here:  ANTHROPIC_BASE_URL=http://127.0.0.1:{port}   or", flush=True)
    print(f"                       OPENAI_BASE_URL=http://127.0.0.1:{port}/v1", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), ProxyHandler).serve_forever()


if __name__ == "__main__":
    serve()
