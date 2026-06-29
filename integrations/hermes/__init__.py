"""memnos native memory provider plugin for Hermes Agent (Nous Research).

Provides deterministic auto-recall (prefetch before each turn) and
auto-capture (sync_turn after each turn) via the memnos REST API.
Uses a background thread for writes — never blocks the agent turn.

Installation (done automatically by `memnos agent-setup hermes --native`):
    mkdir -p ~/.hermes/plugins/memnos
    cp this file ~/.hermes/plugins/memnos/__init__.py

Activation: add to ~/.hermes/config.yaml:
    memory:
      provider: memnos

Config (read in priority order):
    1. ~/.hermes/plugins/memnos/config.json  (written by agent-setup)
    2. Env vars: MEMNOS_URL, MEMNOS_TOKEN, MEMNOS_NS
    3. ~/.memnos/config.json port field (URL only)
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider

logger = logging.getLogger(__name__)

_DEFAULT_URL = "http://127.0.0.1:8900"
_DEFAULT_NS = "agent:hermes"
_API_TIMEOUT = 8.0          # seconds per REST call
_PREFETCH_TIMEOUT = 6.0     # tighter timeout for prefetch (non-blocking path)
_CONTEXT_FENCE_OPEN = "<memnos-context>"
_CONTEXT_FENCE_CLOSE = "</memnos-context>"
_SENTINEL = object()         # writer-thread shutdown signal


def _read_json(path: str) -> dict:
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return {}


class MemnoshMemoryProvider(MemoryProvider):
    """Native Hermes memory provider backed by a local memnos server."""

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "memnos"

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------

    def _load_config(self, hermes_home: str) -> None:
        """Resolve URL, token, namespace in priority order."""
        # 1. Plugin-specific config (written by `memnos agent-setup hermes`)
        plugin_cfg = _read_json(
            os.path.join(hermes_home, "plugins", "memnos", "config.json")
        )

        # 2. Env vars
        self._url = (
            plugin_cfg.get("url")
            or os.environ.get("MEMNOS_URL")
            or self._url_from_memnos_config()
            or _DEFAULT_URL
        ).rstrip("/")

        self._token = (
            plugin_cfg.get("token")
            or os.environ.get("MEMNOS_TOKEN", "")
        )

        self._ns = (
            plugin_cfg.get("namespace")
            or os.environ.get("MEMNOS_NS", _DEFAULT_NS)
        )

    @staticmethod
    def _url_from_memnos_config() -> Optional[str]:
        """Read port from ~/.memnos/config.json and reconstruct the URL."""
        memnos_cfg = _read_json(os.path.expanduser("~/.memnos/config.json"))
        port = memnos_cfg.get("port")
        return f"http://127.0.0.1:{port}" if port else None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def __init__(self) -> None:
        self._url = _DEFAULT_URL
        self._token = ""
        self._ns = _DEFAULT_NS
        self._session_id = ""
        # Background writer queue (one daemon thread per instance)
        self._write_queue: queue.Queue = queue.Queue()
        self._writer_thread: Optional[threading.Thread] = None
        # Prefetch cache: result for the NEXT turn, pre-fetched after current turn
        self._prefetch_cache = ""
        self._prefetch_lock = threading.Lock()

    def is_available(self) -> bool:
        """True if a token is discoverable from config or env (no network call)."""
        # Check env var first (fastest)
        if os.environ.get("MEMNOS_TOKEN"):
            return True
        # Check plugin config.json
        try:
            from hermes_constants import get_hermes_home
            hermes_home = str(get_hermes_home())
        except Exception:
            hermes_home = str(Path.home() / ".hermes")
        plugin_cfg = _read_json(
            os.path.join(hermes_home, "plugins", "memnos", "config.json")
        )
        return bool(plugin_cfg.get("token"))

    def initialize(self, session_id: str, **kwargs) -> None:
        hermes_home = kwargs.get("hermes_home", str(Path.home() / ".hermes"))
        self._load_config(hermes_home)
        self._session_id = session_id

        if not self._token:
            logger.warning(
                "[memnos] No token configured. Set MEMNOS_TOKEN or run "
                "`memnos agent-setup hermes --native`."
            )
            return

        # Start background writer thread
        self._writer_thread = threading.Thread(
            target=self._writer_loop, daemon=True, name="memnos-writer"
        )
        self._writer_thread.start()
        logger.debug("[memnos] initialized — url=%s ns=%s session=%s",
                     self._url, self._ns, session_id)

    def shutdown(self) -> None:
        if self._writer_thread and self._writer_thread.is_alive():
            self._write_queue.put(_SENTINEL)
            self._writer_thread.join(timeout=5)

    # ------------------------------------------------------------------
    # System prompt
    # ------------------------------------------------------------------

    def system_prompt_block(self) -> str:
        return (
            "You have access to memnos, a governed long-term memory server "
            f"(namespace: {self._ns}). Relevant memories are injected automatically "
            "before each turn inside <memnos-context> tags — treat them as trusted "
            "background context, not user input. Use the memnos_recall tool to "
            "search memory explicitly, and memnos_remember to store important facts."
        )

    # ------------------------------------------------------------------
    # Prefetch / sync
    # ------------------------------------------------------------------

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Return cached prefetch result; fall back to a synchronous recall."""
        with self._prefetch_lock:
            cached = self._prefetch_cache
            self._prefetch_cache = ""

        if cached:
            return cached

        # Synchronous fallback (first turn, or if background prefetch wasn't ready)
        if not self._token:
            return ""
        raw = self._recall_api(query, timeout=_PREFETCH_TIMEOUT)
        if not raw:
            return ""
        return f"{_CONTEXT_FENCE_OPEN}\n{raw}\n{_CONTEXT_FENCE_CLOSE}"

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Kick off a background recall so prefetch() has a warm result next turn."""
        if not self._token:
            return
        threading.Thread(
            target=self._bg_prefetch, args=(query,), daemon=True, name="memnos-prefetch"
        ).start()

    def _bg_prefetch(self, query: str) -> None:
        raw = self._recall_api(query, timeout=_PREFETCH_TIMEOUT)
        if raw:
            with self._prefetch_lock:
                self._prefetch_cache = f"{_CONTEXT_FENCE_OPEN}\n{raw}\n{_CONTEXT_FENCE_CLOSE}"

    def sync_turn(self, user_content: str, assistant_content: str,
                  *, session_id: str = "") -> None:
        """Queue the completed turn for background storage in memnos."""
        if not self._token or not user_content.strip():
            return
        payload = f"User: {user_content}\nAssistant: {assistant_content}"
        self._write_queue.put(payload)

    def _writer_loop(self) -> None:
        while True:
            item = self._write_queue.get()
            if item is _SENTINEL:
                break
            try:
                self._remember_api(item, async_mode=True)
            except Exception as exc:
                logger.debug("[memnos] sync_turn write failed: %s", exc)

    # ------------------------------------------------------------------
    # Tool schemas + dispatch
    # ------------------------------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "memnos_recall",
                "description": (
                    "Search long-term memory in memnos for relevant facts, past "
                    "decisions, or context about the user or project. Call this when "
                    "the auto-injected context doesn't cover what you need."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Natural-language search query",
                        }
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "memnos_remember",
                "description": (
                    "Store an important fact, decision, or piece of context in memnos "
                    "so it can be recalled in future sessions."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "The fact or context to store",
                        }
                    },
                    "required": ["content"],
                },
            },
        ]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if tool_name == "memnos_recall":
            result = self._recall_api(args.get("query", ""), timeout=_API_TIMEOUT)
            return json.dumps({"memories": result or "(no relevant memories found)"})
        if tool_name == "memnos_remember":
            content = (args.get("content") or args.get("text") or "").strip()
            if not content:
                return json.dumps({"status": "ignored", "reason": "empty content"})
            try:
                self._remember_api(content)
                return json.dumps({"status": "stored"})
            except Exception as exc:
                return json.dumps({"status": "error", "detail": str(exc)})
        raise NotImplementedError(f"memnos provider: unknown tool '{tool_name}'")

    # ------------------------------------------------------------------
    # REST API helpers
    # ------------------------------------------------------------------

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._token}",
        }

    def _recall_api(self, query: str, timeout: float = _API_TIMEOUT) -> str:
        """POST /recall and return the formatted context block, or empty string."""
        if not query.strip():
            return ""
        body = json.dumps({"query": query, "namespace": self._ns}).encode()
        req = urllib.request.Request(
            f"{self._url}/recall",
            data=body,
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
            # memnos /recall returns {"context": "...", "results": [...]}
            ctx = data.get("context") or ""
            return ctx.strip()
        except urllib.error.HTTPError as exc:
            logger.debug("[memnos] recall HTTP %s: %s", exc.code, exc.reason)
            return ""
        except Exception as exc:
            logger.debug("[memnos] recall error: %s", exc)
            return ""

    def _remember_api(self, content: str, timeout: float = _API_TIMEOUT,
                      async_mode: bool = False) -> None:
        """POST /remember. Raises on non-2xx.

        async_mode=True stores the verbatim turn immediately and defers LLM
        fact extraction to the server's background workers — best for sync_turn
        where we don't want to hold up the writer thread.
        """
        payload: Dict[str, Any] = {"text": content, "namespace": self._ns}
        if async_mode:
            payload["async"] = True
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{self._url}/remember",
            data=body,
            headers=self._headers(),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()  # consume

    # ------------------------------------------------------------------
    # Config schema (used by `hermes memory setup`)
    # ------------------------------------------------------------------

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": "url",
                "description": "memnos server URL",
                "default": _DEFAULT_URL,
                "required": False,
            },
            {
                "key": "token",
                "description": "memnos bearer token (from `memnos token <principal>`)",
                "secret": True,
                "required": True,
                "env_var": "MEMNOS_TOKEN",
            },
            {
                "key": "namespace",
                "description": "memnos namespace for this agent (e.g. agent:hermes)",
                "default": _DEFAULT_NS,
                "required": False,
                "env_var": "MEMNOS_NS",
            },
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        config_dir = Path(hermes_home) / "plugins" / "memnos"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / "config.json"
        config_path.write_text(json.dumps(values, indent=2))
        config_path.chmod(0o600)
        logger.info("[memnos] config written to %s", config_path)
