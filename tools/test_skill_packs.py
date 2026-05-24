"""
tools/test_skill_packs.py — Unit tests for external MCP skill pack loader.

Tests cover:
- load_skill_packs() with missing / empty directory
- YAML pack file parsing (valid, malformed, missing fields)
- JSON pack file parsing
- Tool name collision detection
- _parse_handler() — webhook only, unsupported types
- call_webhook_handler() — success, timeout, HTTP error, JSON response
- server._dispatch() routing to skill pack handlers
"""
from __future__ import annotations

import json
import sys
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, "/Users/thameema/git/engram/packages/mcp-server")
sys.path.insert(0, "/Users/thameema/git/engram/packages/api")
sys.path.insert(0, "/Users/thameema/git/engram/packages/core")

from engram_mcp.skill_packs import (
    WebhookHandler,
    SkillPackEntry,
    _load_pack_file,
    _parse_handler,
    load_skill_packs,
    call_webhook_handler,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_pack(tmpdir: Path, filename: str, content: str) -> Path:
    p = tmpdir / filename
    p.write_text(content, encoding="utf-8")
    return p


VALID_YAML = textwrap.dedent("""\
    name: weather-tools
    version: "1.0"
    tools:
      - name: weather_lookup
        description: Look up current weather
        inputSchema:
          type: object
          properties:
            city:
              type: string
          required: [city]
        handler:
          type: webhook
          url: https://weather.internal/mcp
          timeout_s: 15
          headers:
            X-Api-Key: secret123
""")

VALID_JSON = json.dumps({
    "name": "calc-tools",
    "tools": [
        {
            "name": "calculator",
            "description": "Simple calculator",
            "inputSchema": {"type": "object", "properties": {"expr": {"type": "string"}}, "required": ["expr"]},
            "handler": {"type": "webhook", "url": "https://calc.internal/eval"},
        }
    ],
})


# ---------------------------------------------------------------------------
# load_skill_packs
# ---------------------------------------------------------------------------

class TestLoadSkillPacksDirectory(unittest.TestCase):
    def test_missing_dir_returns_empty(self):
        result = load_skill_packs("/nonexistent/path/that/doesnt/exist")
        self.assertEqual(result, [])

    def test_empty_dir_returns_empty(self):
        with TemporaryDirectory() as tmp:
            result = load_skill_packs(tmp)
            self.assertEqual(result, [])

    def test_valid_yaml_pack_loaded(self):
        with TemporaryDirectory() as tmp:
            _write_pack(Path(tmp), "weather.yaml", VALID_YAML)
            entries = load_skill_packs(tmp)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].tool.name, "weather_lookup")

    def test_valid_json_pack_loaded(self):
        with TemporaryDirectory() as tmp:
            _write_pack(Path(tmp), "calc.json", VALID_JSON)
            entries = load_skill_packs(tmp)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].tool.name, "calculator")

    def test_malformed_yaml_skipped_gracefully(self):
        with TemporaryDirectory() as tmp:
            _write_pack(Path(tmp), "bad.yaml", "not: a: valid: yaml: [[[")
            entries = load_skill_packs(tmp)
            self.assertEqual(entries, [])

    def test_multiple_packs_loaded_together(self):
        with TemporaryDirectory() as tmp:
            _write_pack(Path(tmp), "a.yaml", VALID_YAML)
            _write_pack(Path(tmp), "b.json", VALID_JSON)
            entries = load_skill_packs(tmp)
            names = [e.tool.name for e in entries]
            self.assertIn("weather_lookup", names)
            self.assertIn("calculator", names)

    def test_env_var_used_as_default_dir(self):
        with TemporaryDirectory() as tmp:
            _write_pack(Path(tmp), "w.yaml", VALID_YAML)
            with patch.dict("os.environ", {"ENGRAM_SKILL_PACKS_DIR": tmp}):
                entries = load_skill_packs()
            self.assertEqual(len(entries), 1)

    def test_tool_name_collision_skipped(self):
        with TemporaryDirectory() as tmp:
            _write_pack(Path(tmp), "w.yaml", VALID_YAML)
            # "weather_lookup" is already known
            entries = load_skill_packs(tmp, known_names={"weather_lookup"})
            self.assertEqual(entries, [])

    def test_pack_name_recorded(self):
        with TemporaryDirectory() as tmp:
            _write_pack(Path(tmp), "weather.yaml", VALID_YAML)
            entries = load_skill_packs(tmp)
            self.assertEqual(entries[0].pack_name, "weather-tools")


# ---------------------------------------------------------------------------
# _load_pack_file
# ---------------------------------------------------------------------------

class TestLoadPackFile(unittest.TestCase):
    def test_not_a_dict_raises(self):
        with TemporaryDirectory() as tmp:
            p = Path(tmp) / "bad.yaml"
            p.write_text("- item1\n- item2\n")
            with self.assertRaises(ValueError, msg="top-level must be dict"):
                _load_pack_file(p, known_names=set())

    def test_missing_tools_list_raises(self):
        with TemporaryDirectory() as tmp:
            p = Path(tmp) / "no_tools.yaml"
            p.write_text("name: empty-pack\n")
            with self.assertRaises(ValueError):
                _load_pack_file(p, known_names=set())

    def test_tool_without_name_skipped(self):
        content = textwrap.dedent("""\
            name: test
            tools:
              - description: no name here
                inputSchema:
                  type: object
                handler:
                  type: webhook
                  url: https://example.com
        """)
        with TemporaryDirectory() as tmp:
            p = Path(tmp) / "nameless.yaml"
            p.write_text(content)
            # parse errors at the individual tool level are swallowed
            entries = _load_pack_file(p, known_names=set())
            self.assertEqual(entries, [])

    def test_tool_missing_handler_skipped(self):
        content = textwrap.dedent("""\
            name: test
            tools:
              - name: my_tool
                description: no handler
                inputSchema:
                  type: object
        """)
        with TemporaryDirectory() as tmp:
            p = Path(tmp) / "nohandler.yaml"
            p.write_text(content)
            entries = _load_pack_file(p, known_names=set())
            self.assertEqual(entries, [])

    def test_default_input_schema_when_missing(self):
        content = textwrap.dedent("""\
            name: test
            tools:
              - name: minimal_tool
                description: bare minimum
                handler:
                  type: webhook
                  url: https://example.com
        """)
        with TemporaryDirectory() as tmp:
            p = Path(tmp) / "minimal.yaml"
            p.write_text(content)
            entries = _load_pack_file(p, known_names=set())
            self.assertEqual(len(entries), 1)
            self.assertIn("type", entries[0].tool.inputSchema)


# ---------------------------------------------------------------------------
# _parse_handler
# ---------------------------------------------------------------------------

class TestParseHandler(unittest.TestCase):
    def test_valid_webhook(self):
        h = _parse_handler("my_tool", {"type": "webhook", "url": "https://example.com", "timeout_s": 20})
        self.assertIsInstance(h, WebhookHandler)
        self.assertEqual(h.url, "https://example.com")
        self.assertEqual(h.timeout_s, 20)

    def test_default_timeout(self):
        h = _parse_handler("t", {"type": "webhook", "url": "https://x.com"})
        self.assertEqual(h.timeout_s, 30)

    def test_headers_parsed(self):
        h = _parse_handler("t", {"type": "webhook", "url": "https://x.com", "headers": {"X-Key": "val"}})
        self.assertEqual(h.headers, {"X-Key": "val"})

    def test_unsupported_type_raises(self):
        with self.assertRaises(ValueError, msg="only 'webhook' is supported"):
            _parse_handler("t", {"type": "python", "module": "my.mod", "function": "run"})

    def test_missing_url_raises(self):
        with self.assertRaises(ValueError):
            _parse_handler("t", {"type": "webhook"})

    def test_empty_url_raises(self):
        with self.assertRaises(ValueError):
            _parse_handler("t", {"type": "webhook", "url": ""})


# ---------------------------------------------------------------------------
# call_webhook_handler
# ---------------------------------------------------------------------------

class TestCallWebhookHandler(unittest.IsolatedAsyncioTestCase):
    def _handler(self, url="https://example.com", timeout_s=10, headers=None):
        return WebhookHandler(url=url, timeout_s=timeout_s, headers=headers or {})

    async def test_success_text_response(self):
        mock_resp = MagicMock()
        mock_resp.is_success = True
        mock_resp.headers = {"content-type": "text/plain"}
        mock_resp.text = "42 degrees"

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await call_webhook_handler(self._handler(), "weather_lookup", {"city": "NYC"})

        self.assertEqual(result, "42 degrees")

    async def test_success_json_response_dict(self):
        mock_resp = MagicMock()
        mock_resp.is_success = True
        mock_resp.headers = {"content-type": "application/json"}
        mock_resp.json = MagicMock(return_value={"temp": 22, "unit": "C"})
        mock_resp.text = '{"temp":22,"unit":"C"}'

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await call_webhook_handler(self._handler(), "w", {})

        self.assertIn("temp", result)
        self.assertIn("22", result)

    async def test_success_json_response_string(self):
        mock_resp = MagicMock()
        mock_resp.is_success = True
        mock_resp.headers = {"content-type": "application/json"}
        mock_resp.json = MagicMock(return_value="plain string from json")

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await call_webhook_handler(self._handler(), "w", {})

        self.assertEqual(result, "plain string from json")

    async def test_http_error_raises_runtime_error(self):
        mock_resp = MagicMock()
        mock_resp.is_success = False
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            with self.assertRaises(RuntimeError) as ctx:
                await call_webhook_handler(self._handler(), "my_tool", {})

        self.assertIn("500", str(ctx.exception))

    async def test_timeout_raises_runtime_error(self):
        import httpx

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timed out"))

        with patch("httpx.AsyncClient", return_value=mock_client):
            with self.assertRaises(RuntimeError) as ctx:
                await call_webhook_handler(self._handler(), "my_tool", {})

        self.assertIn("timed out", str(ctx.exception).lower())

    async def test_request_error_raises_runtime_error(self):
        import httpx

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(side_effect=httpx.RequestError("connection refused"))

        with patch("httpx.AsyncClient", return_value=mock_client):
            with self.assertRaises(RuntimeError) as ctx:
                await call_webhook_handler(self._handler(), "my_tool", {})

        self.assertIn("connection refused", str(ctx.exception).lower())

    async def test_headers_forwarded(self):
        mock_resp = MagicMock()
        mock_resp.is_success = True
        mock_resp.headers = {}
        mock_resp.text = "ok"

        captured = {}
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        async def fake_post(url, json=None, headers=None):
            captured["headers"] = headers
            return mock_resp

        mock_client.post = fake_post

        handler = self._handler(headers={"X-My-Key": "abc"})
        with patch("httpx.AsyncClient", return_value=mock_client):
            await call_webhook_handler(handler, "t", {})

        self.assertEqual(captured["headers"].get("X-My-Key"), "abc")

    async def test_httpx_not_installed_raises(self):
        with patch.dict("sys.modules", {"httpx": None}):
            with self.assertRaises(RuntimeError) as ctx:
                await call_webhook_handler(self._handler(), "t", {})
        self.assertIn("httpx", str(ctx.exception))


# ---------------------------------------------------------------------------
# Server _dispatch routing to skill packs
# ---------------------------------------------------------------------------

class TestServerDispatchSkillPacks(unittest.IsolatedAsyncioTestCase):
    async def test_dispatch_routes_to_skill_pack(self):
        import engram_mcp.server as srv

        handler = WebhookHandler(url="https://example.com")
        orig_handlers = dict(srv._SKILL_PACK_HANDLERS)
        srv._SKILL_PACK_HANDLERS["external_tool"] = handler

        try:
            with patch("engram_mcp.skill_packs.call_webhook_handler", new=AsyncMock(return_value="hello from pack")) as mock_call:
                result = await srv._dispatch("external_tool", {"x": 1}, MagicMock(), MagicMock())
            mock_call.assert_awaited_once_with(handler, "external_tool", {"x": 1})
            from mcp.types import TextContent
            self.assertEqual(result[0].text, "hello from pack")
        finally:
            srv._SKILL_PACK_HANDLERS.clear()
            srv._SKILL_PACK_HANDLERS.update(orig_handlers)

    async def test_dispatch_unknown_tool_raises(self):
        import engram_mcp.server as srv

        with self.assertRaises(ValueError) as ctx:
            await srv._dispatch("totally_unknown_xyz", {}, MagicMock(), MagicMock())

        self.assertIn("totally_unknown_xyz", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
