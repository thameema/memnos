"""
engram_mcp.skill_packs — External MCP skill pack loader.

Loads additional MCP tool definitions from YAML/JSON files at startup so
users can extend the MCP server without modifying Python source.

Skill pack file format (YAML example)
--------------------------------------
name: my-tools
version: "1.0"
tools:
  - name: weather_lookup
    description: "Look up current weather for a city"
    inputSchema:
      type: object
      properties:
        city:
          type: string
          description: "City name"
      required: [city]
    handler:
      type: webhook
      url: "https://my-service.internal/mcp/weather_lookup"
      timeout_s: 15          # optional, default 30
      headers:               # optional extra headers
        X-Api-Key: "secret"

Directory discovery
-------------------
The loader scans ``ENGRAM_SKILL_PACKS_DIR`` (default: ``./skill_packs/``)
for ``*.yaml`` and ``*.json`` files. Files that fail to parse are skipped
with a WARNING — a bad pack never prevents the server from starting.

Tool name conflicts
-------------------
If a pack defines a tool whose name already exists in the built-in
catalogue, the pack's definition is silently skipped.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from mcp.types import Tool

logger = logging.getLogger(__name__)

_DEFAULT_PACKS_DIR = "skill_packs"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class WebhookHandler:
    url: str
    timeout_s: int = 30
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class SkillPackEntry:
    tool: Tool
    handler: WebhookHandler
    pack_name: str = ""


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def load_skill_packs(
    packs_dir: str | Path | None = None,
    *,
    known_names: set[str] | None = None,
) -> list[SkillPackEntry]:
    """
    Scan *packs_dir* for ``.yaml``/``.json`` skill pack files and return
    a list of :class:`SkillPackEntry` objects.

    Parameters
    ----------
    packs_dir:
        Directory to scan.  Defaults to ``ENGRAM_SKILL_PACKS_DIR`` env var,
        falling back to ``./skill_packs/``.
    known_names:
        Set of already-registered tool names; any tool whose name collides
        with an existing entry is skipped.
    """
    if packs_dir is None:
        packs_dir = os.environ.get("ENGRAM_SKILL_PACKS_DIR", _DEFAULT_PACKS_DIR)
    packs_dir = Path(packs_dir)

    if not packs_dir.is_dir():
        logger.debug("Skill packs dir %s not found — no external tools loaded", packs_dir)
        return []

    known = known_names or set()
    entries: list[SkillPackEntry] = []

    pack_files = sorted(packs_dir.glob("*.yaml")) + sorted(packs_dir.glob("*.yml")) + sorted(packs_dir.glob("*.json"))
    for path in pack_files:
        try:
            new_entries = _load_pack_file(path, known_names=known)
            for entry in new_entries:
                known.add(entry.tool.name)
            entries.extend(new_entries)
        except Exception as exc:
            logger.warning("Skipping skill pack %s — parse error: %s", path.name, exc)

    if entries:
        logger.info("Loaded %d external tool(s) from %d skill pack file(s)", len(entries), len(pack_files))
    return entries


def _load_pack_file(path: Path, *, known_names: set[str]) -> list[SkillPackEntry]:
    """Parse a single skill pack file; raise on format errors."""
    raw_text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        data = json.loads(raw_text)
    else:
        data = yaml.safe_load(raw_text)

    if not isinstance(data, dict):
        raise ValueError("pack file must be a YAML/JSON object at the top level")

    pack_name = str(data.get("name") or path.stem)
    raw_tools = data.get("tools")
    if not isinstance(raw_tools, list):
        raise ValueError(f"pack '{pack_name}' has no 'tools' list")

    entries: list[SkillPackEntry] = []
    for i, raw_tool in enumerate(raw_tools):
        if not isinstance(raw_tool, dict):
            logger.warning("pack '%s' tool[%d] is not a dict — skipped", pack_name, i)
            continue
        try:
            entry = _parse_tool_entry(raw_tool, pack_name=pack_name, known_names=known_names)
            if entry is not None:
                entries.append(entry)
        except Exception as exc:
            name_hint = raw_tool.get("name", f"[{i}]")
            logger.warning("pack '%s' tool '%s' — parse error: %s", pack_name, name_hint, exc)

    return entries


def _parse_tool_entry(
    raw: dict[str, Any],
    *,
    pack_name: str,
    known_names: set[str],
) -> SkillPackEntry | None:
    name = str(raw.get("name") or "").strip()
    if not name:
        raise ValueError("tool entry missing 'name'")

    if name in known_names:
        logger.debug("pack '%s' tool '%s' conflicts with existing tool — skipped", pack_name, name)
        return None

    description = str(raw.get("description") or "").strip()
    input_schema = raw.get("inputSchema") or raw.get("input_schema") or {"type": "object", "properties": {}, "required": []}
    if not isinstance(input_schema, dict):
        raise ValueError(f"tool '{name}' inputSchema must be a dict")

    handler_raw = raw.get("handler")
    if not isinstance(handler_raw, dict):
        raise ValueError(f"tool '{name}' missing 'handler' dict")

    handler = _parse_handler(name, handler_raw)

    tool = Tool(name=name, description=description, inputSchema=input_schema)
    return SkillPackEntry(tool=tool, handler=handler, pack_name=pack_name)


def _parse_handler(tool_name: str, raw: dict[str, Any]) -> WebhookHandler:
    handler_type = str(raw.get("type") or "").lower()
    if handler_type != "webhook":
        raise ValueError(
            f"tool '{tool_name}' handler type '{handler_type}' is not supported — "
            "only 'webhook' is accepted"
        )
    url = str(raw.get("url") or "").strip()
    if not url:
        raise ValueError(f"tool '{tool_name}' webhook handler missing 'url'")

    timeout_s = int(raw.get("timeout_s") or 30)
    headers = dict(raw.get("headers") or {})
    return WebhookHandler(url=url, timeout_s=timeout_s, headers=headers)


# ---------------------------------------------------------------------------
# Webhook dispatcher
# ---------------------------------------------------------------------------

async def call_webhook_handler(
    handler: WebhookHandler,
    tool_name: str,
    arguments: dict[str, Any],
) -> str:
    """
    POST ``{"tool": tool_name, "arguments": arguments}`` to the webhook URL.

    Returns the response body as a string. Raises :class:`RuntimeError` on
    HTTP errors so the MCP caller receives a clean error message.
    """
    try:
        import httpx
    except ImportError:
        raise RuntimeError(
            "httpx is required for webhook skill packs. "
            "Install it with: pip install httpx"
        )

    payload = {"tool": tool_name, "arguments": arguments}
    logger.debug("skill_pack webhook | tool=%s url=%s", tool_name, handler.url)

    try:
        async with httpx.AsyncClient(timeout=handler.timeout_s) as http:
            resp = await http.post(handler.url, json=payload, headers=handler.headers)
    except httpx.TimeoutException as exc:
        raise RuntimeError(
            f"Webhook tool '{tool_name}' timed out after {handler.timeout_s}s"
        ) from exc
    except httpx.RequestError as exc:
        raise RuntimeError(
            f"Webhook tool '{tool_name}' request failed: {exc}"
        ) from exc

    if not resp.is_success:
        raise RuntimeError(
            f"Webhook tool '{tool_name}' returned HTTP {resp.status_code}: {resp.text[:200]}"
        )

    content_type = resp.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            body = resp.json()
            if isinstance(body, str):
                return body
            return json.dumps(body, ensure_ascii=False)
        except Exception:
            pass
    return resp.text
