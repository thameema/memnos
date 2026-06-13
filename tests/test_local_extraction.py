"""Local LLM extraction routing (feature c6346aa, $0 — no OpenAI).

Pins the MEMNOS_EXTRACT_BASE_URL contract: a self-hosted, OpenAI-compatible endpoint
(Ollama / vLLM / LM Studio) does FACT EXTRACTION while embeddings stay on the free,
private local-384 path. Concretely:

  1. _build_embedder() with MEMNOS_EXTRACT_BASE_URL set and NO OPENAI_API_KEY:
       - DIM stays 384 (local embeddings, not 1536 OpenAI)
       - LLM is wired to the custom base_url with the MEMNOS_EXTRACT_MODEL
       - the returned embedder is the LOCAL one (local_models.embed), NOT a CachedEmbedder
         around an OpenAI client (so embeddings NEVER call OpenAI).
  2. A real extraction (service.extract_facts) routes its chat call to the FAKE endpoint
     with the configured model — and the embeddings route is NEVER hit.

No network to any real provider; the only HTTP server is the in-process fake. $0.
Run: python tests/test_local_extraction.py
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

PASS = FAIL = 0
seen = []                       # (path, model) the fake OpenAI-compatible endpoint received
FAKE_PORT = 8941


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += cond; FAIL += (not cond)


class FakeOpenAICompatible(BaseHTTPRequestHandler):
    """Minimal Ollama/vLLM-style server: only the chat endpoint is implemented; a hit on
    /embeddings would mean embeddings wrongly went to the LLM endpoint (a regression)."""
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
        seen.append((self.path, body.get("model")))
        if self.path.endswith("/chat/completions"):
            content = json.dumps({"facts": [
                {"subject": "user", "predicate": "lives_in", "object": "Frisco",
                 "statement": "The user lives in Frisco."}]})
            out = json.dumps({
                "id": "chatcmpl-fake", "object": "chat.completion", "model": body.get("model"),
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }).encode()
        else:                                   # /embeddings or anything else — should NOT happen
            out = json.dumps({"object": "list", "data": [], "model": body.get("model")}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers(); self.wfile.write(out)


def main():
    print("=== local LLM extraction routing (MEMNOS_EXTRACT_BASE_URL) ===")
    srv = ThreadingHTTPServer(("127.0.0.1", FAKE_PORT), FakeOpenAICompatible)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    base = f"http://127.0.0.1:{FAKE_PORT}/v1"
    model = "llama3.2:3b"
    # No OpenAI: embeddings must stay local-384, only extraction uses the custom endpoint.
    # Empty string (not pop) so memnos_server._load_env()'s setdefault can't repopulate it
    # from a repo .env on reload — empty is falsy, so _build_embedder takes the local path.
    os.environ["OPENAI_API_KEY"] = ""
    os.environ["MEMNOS_EXTRACT_BASE_URL"] = base
    os.environ["MEMNOS_EXTRACT_MODEL"] = model

    import importlib
    import memnos_server
    importlib.reload(memnos_server)             # re-read EXTRACT_MODEL from the env we just set
    from core import local_models

    emb = memnos_server._build_embedder()

    check("embeddings stay local-384 (DIM == 384, not 1536)", memnos_server.DIM == 384)
    check("embedder is the LOCAL embedder (no OpenAI embeddings client)",
          emb is local_models.embed)
    check("LLM wired to the custom base_url",
          memnos_server.LLM is not None
          and str(memnos_server.LLM.base_url).rstrip("/") == base.rstrip("/"))
    check("extract model is MEMNOS_EXTRACT_MODEL", memnos_server.EXTRACT_MODEL == model)

    # Drive a REAL extraction through the production service using the wired LLM.
    from core.service import MemnosMemory
    mem = MemnosMemory(None, emb, dim=memnos_server.DIM, llm=memnos_server.LLM,
                       extract_model=memnos_server.EXTRACT_MODEL)
    facts = mem.extract_facts("The user lives in Frisco, Texas.", "2026-06-13")

    chat_calls = [m for (p, m) in seen if p.endswith("/chat/completions")]
    embed_calls = [p for (p, m) in seen if p.endswith("/embeddings")]
    check("extraction chat call routed to the custom endpoint", len(chat_calls) == 1)
    check("extraction used the configured model", bool(chat_calls) and chat_calls[0] == model)
    check("extraction returned the endpoint's facts",
          any("Frisco" in f.get("statement", "") for f in facts))
    check("embeddings NEVER hit the LLM endpoint (stay local/free)", embed_calls == [])

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
