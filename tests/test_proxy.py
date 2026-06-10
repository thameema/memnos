"""Contract tests for `memnos proxy` (memnos_proxy.py) — fake upstream + fake memnos
server, no real providers and no DB. Pins the capture-tier contract:

  relay: byte-faithful passthrough (status, body, streaming), BYOK header forwarding,
         upstream errors relayed verbatim, capture failure NEVER breaks relay
  capture: both speakers saved for terminal human exchanges; tool-call loop iterations,
           denylisted (haiku/title) models, tiny-max_tokens calls and resent duplicates
           are all skipped

Run: python tests/test_proxy.py
"""
import json
import os
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

PASS = FAIL = 0
upstream_seen = []      # (path, headers, body) the fake provider received
remembered = []         # /remember bodies the fake memnos received
UPSTREAM_PORT, MEMNOS_PORT, PROXY_PORT = 8931, 8932, 8933


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += cond; FAIL += (not cond)


class FakeUpstream(BaseHTTPRequestHandler):
    """Plays OpenAI or Anthropic depending on path; behavior keyed off request content."""
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        upstream_seen.append((self.path, dict(self.headers), body))
        marker = json.dumps(body)
        if "UPSTREAM_500" in marker:
            err = b'{"error": {"message": "upstream exploded"}}'
            self.send_response(500)
            self.send_header("Content-Length", str(len(err)))
            self.end_headers(); self.wfile.write(err); return
        if self.path.startswith("/v1/messages"):
            self._anthropic(body, marker)
        else:
            self._openai(body, marker)

    def _openai(self, body, marker):
        wants_tools = "CALL_A_TOOL" in marker
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            chunks = [
                {"choices": [{"delta": {"content": "Streamed answer: ticket "}, "finish_reason": None}]},
                {"choices": [{"delta": {"content": "HPTE-467 is the anchor."}, "finish_reason": None}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            ]
            for c in chunks:
                d = f"data: {json.dumps(c)}\n\n".encode()
                self.wfile.write(f"{len(d):X}\r\n".encode() + d + b"\r\n"); self.wfile.flush()
                time.sleep(0.05)
            done = b"data: [DONE]\n\n"
            self.wfile.write(f"{len(done):X}\r\n".encode() + done + b"\r\n0\r\n\r\n")
            return
        last = body.get("messages", [{}])[-1].get("content", "")
        msg = ({"content": None, "tool_calls": [{"id": "t1"}]} if wants_tools
               else {"content": f"Final answer re '{str(last)[:48]}': we chose pgvector over a graph DB."})
        out = json.dumps({"choices": [{"message": msg,
                                       "finish_reason": "tool_calls" if wants_tools else "stop"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers(); self.wfile.write(out)

    def _anthropic(self, body, marker):
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            evs = [
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Decision recorded: "}},
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "HPTE-471 and HPTE-472 raised."}},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
            ]
            for e in evs:
                d = f"data: {json.dumps(e)}\n\n".encode()
                self.wfile.write(f"{len(d):X}\r\n".encode() + d + b"\r\n"); self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            return
        out = json.dumps({"content": [{"type": "text", "text": "Non-stream anthropic reply with details."}],
                          "stop_reason": "end_turn"}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers(); self.wfile.write(out)


class FakeMemnos(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        b = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if "MEMNOS_DOWN" in b.get("text", ""):
            self.send_response(500); self.send_header("Content-Length", "0"); self.end_headers(); return
        remembered.append(b)
        out = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers(); self.wfile.write(out)


def post(path, body, headers=None, port=PROXY_PORT):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="POST",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def wait_capture(n, timeout=5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if len(remembered) >= n:
            return True
        time.sleep(0.05)
    return False


def user_msgs(text):
    return [{"role": "user", "content": text}]


def main():
    print("=== memnos proxy contracts ===")
    for cls, port in ((FakeUpstream, UPSTREAM_PORT), (FakeMemnos, MEMNOS_PORT)):
        srv = ThreadingHTTPServer(("127.0.0.1", port), cls)
        threading.Thread(target=srv.serve_forever, daemon=True).start()

    import memnos_proxy
    memnos_proxy.CFG.update({
        "port": PROXY_PORT, "capture": True,
        "upstreams": {"openai": f"http://127.0.0.1:{UPSTREAM_PORT}",
                      "anthropic": f"http://127.0.0.1:{UPSTREAM_PORT}"},
        "server_url": f"http://127.0.0.1:{MEMNOS_PORT}", "token": "mnk_test",
        "namespace": "user:test",
        "capture_model_denylist": ["*haiku*"], "capture_min_max_tokens": 512,
    })
    threading.Thread(target=memnos_proxy.serve, kwargs={"port": PROXY_PORT}, daemon=True).start()
    time.sleep(0.5)

    # 1. non-streaming OpenAI: relay + both speakers captured
    remembered.clear()
    st, body = post("/v1/chat/completions",
                    {"model": "gpt-5", "messages": user_msgs("which database engine did we finally choose?")},
                    {"Authorization": "Bearer sk-USERKEY"})
    check("openai non-stream: 200 relayed with upstream body",
          st == 200 and b"pgvector" in body)
    check("openai non-stream: both speakers captured",
          wait_capture(2) and [r["speaker"] for r in remembered] == ["user", "assistant"]
          and "pgvector" in remembered[1]["text"])
    check("capture has session_id (episode grouping)", bool(remembered[0].get("session_id")))

    # 2. BYOK: upstream got the exact auth header; x-memnos-namespace stripped
    seen_headers = upstream_seen[-1][1]
    check("auth header forwarded verbatim", seen_headers.get("Authorization") == "Bearer sk-USERKEY")
    remembered.clear();
    post("/v1/chat/completions",
         {"model": "gpt-5", "messages": user_msgs("namespace routing question for the proxy test?")},
         {"x-memnos-namespace": "proj:alpha"})
    check("x-memnos-namespace stripped from upstream",
          "x-memnos-namespace" not in {k.lower() for k in upstream_seen[-1][1]})
    check("namespace header routes capture", wait_capture(2) and remembered[0]["namespace"] == "proj:alpha")

    # 3. streaming OpenAI SSE: relay + accumulate
    remembered.clear()
    st, body = post("/v1/chat/completions",
                    {"model": "gpt-5", "stream": True,
                     "messages": user_msgs("what is the anchor ticket for the consent work?")})
    check("openai stream: SSE relayed", st == 200 and b"data:" in body and b"[DONE]" in body)
    check("openai stream: accumulated reply captured with identifier",
          wait_capture(2) and "HPTE-467" in remembered[1]["text"])

    # 4. streaming Anthropic SSE
    remembered.clear()
    st, body = post("/v1/messages",
                    {"model": "claude-opus-4-8", "stream": True,
                     "messages": user_msgs("please raise the two follow-up tickets now")})
    check("anthropic stream: relayed + captured",
          st == 200 and wait_capture(2) and "HPTE-471" in remembered[1]["text"])

    # 5. tool-call loop iterations are NOT captured
    remembered.clear()
    post("/v1/chat/completions",                       # response ends in tool_calls
         {"model": "gpt-5", "messages": user_msgs("CALL_A_TOOL please look this up for me")})
    post("/v1/chat/completions",                       # request ends with tool_result content
         {"model": "gpt-5", "messages": [
             {"role": "user", "content": "original question with enough words here"},
             {"role": "assistant", "content": None, "tool_calls": [{"id": "t1"}]},
             {"role": "tool", "content": "tool says 42"}]})
    time.sleep(1.0)
    check("tool-loop iterations: zero captures", len(remembered) == 0)

    # 6. denylisted model + tiny max_tokens dropped (relay still fine)
    remembered.clear()
    st, _ = post("/v1/messages", {"model": "claude-haiku-4-5",
                                  "messages": user_msgs("generate a title for this conversation")})
    st2, _ = post("/v1/chat/completions", {"model": "gpt-5", "max_tokens": 64,
                                           "messages": user_msgs("summarize the topic in five words")})
    time.sleep(1.0)
    check("haiku + tiny-max_tokens: relayed but not captured",
          st == 200 and st2 == 200 and len(remembered) == 0)

    # 7. dedupe: identical request twice → one capture pair
    remembered.clear()
    q = {"model": "gpt-5", "messages": user_msgs("does the dedupe layer collapse identical sends?")}
    post("/v1/chat/completions", q); post("/v1/chat/completions", q)
    time.sleep(1.0)
    check("dedupe: 2 relays → 1 capture pair", len(remembered) == 2)

    # 8. upstream 500 relayed verbatim, no capture
    remembered.clear()
    st, body = post("/v1/chat/completions",
                    {"model": "gpt-5", "messages": user_msgs("UPSTREAM_500 trigger an error there")})
    check("upstream 500: status + body relayed, no capture",
          st == 500 and b"upstream exploded" in body and not wait_capture(1, 0.8))

    # 9. capture failure does not break relay (fake memnos 500s on marker)
    remembered.clear()
    st, body = post("/v1/chat/completions",
                    {"model": "gpt-5", "messages": user_msgs("MEMNOS_DOWN but the relay must still work")})
    check("memnos down: relay still 200", st == 200 and b"pgvector" in body)

    # 10. healthz reports stats
    with urllib.request.urlopen(f"http://127.0.0.1:{PROXY_PORT}/healthz", timeout=5) as r:
        h = json.load(r)
    check("healthz: ok + stats", h.get("ok") and "stats" in h)

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
