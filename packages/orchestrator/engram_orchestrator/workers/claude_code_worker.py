"""
engram_orchestrator.workers.claude_code_worker — Worker that spawns the `claude` CLI.

Writes a temporary MCP config pointing at the local engram MCP server,
then invokes `claude --dangerously-skip-permissions --print -p "<PROMPT>"`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any

from .base import BaseWorker

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 300  # seconds


class ClaudeCodeWorker(BaseWorker):
    """Worker that spawns the `claude` CLI subprocess with an engram MCP config."""

    def __init__(
        self,
        api_key: str,
        mcp_server_url: str,
        namespace: str,
        model: str = "claude-sonnet-4-6",
        timeout_s: int = _DEFAULT_TIMEOUT,
    ) -> None:
        self.worker_id = str(uuid.uuid4())
        self._api_key = api_key
        self._mcp_server_url = mcp_server_url
        self._namespace = namespace
        self._model = model
        self.timeout_s = timeout_s
        self._process: asyncio.subprocess.Process | None = None

    # ------------------------------------------------------------------
    # MCP config helpers
    # ------------------------------------------------------------------

    def _build_mcp_config(self, config_path: str) -> None:
        """Write a temporary MCP config JSON file pointing at the engram MCP server."""
        # Determine transport type from URL
        if self._mcp_server_url.startswith("http"):
            server_config: dict[str, Any] = {
                "mcpServers": {
                    "engram": {
                        "transport": "sse",
                        "url": self._mcp_server_url,
                    }
                }
            }
        else:
            # Assume stdio — mcp_server_url is a command path
            server_config = {
                "mcpServers": {
                    "engram": {
                        "command": self._mcp_server_url,
                        "args": ["--transport", "stdio"],
                        "env": {
                            "ENGRAM_NAMESPACE": self._namespace,
                        },
                    }
                }
            }

        with open(config_path, "w", encoding="utf-8") as fh:
            json.dump(server_config, fh, indent=2)

    # ------------------------------------------------------------------
    # Main run
    # ------------------------------------------------------------------

    async def run(self, task_prompt: str, system_prompt: str | None = None) -> str:
        """
        Spawn the `claude` CLI with:
          - A temporary MCP config pointing at the engram MCP server
          - ANTHROPIC_API_KEY from the provided key
          - --dangerously-skip-permissions for headless operation
          - --print to run non-interactively and print to stdout
        """
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".json",
            prefix="engram_mcp_",
            delete=False,
        ) as tmpfile:
            mcp_config_path = tmpfile.name

        try:
            self._build_mcp_config(mcp_config_path)

            # Build the full prompt, optionally prepending a system section
            if system_prompt:
                full_prompt = f"<system>\n{system_prompt}\n</system>\n\n{task_prompt}"
            else:
                full_prompt = task_prompt

            cmd = [
                "claude",
                "--dangerously-skip-permissions",
                "--print",
                "--model",
                self._model,
                "--mcp-config",
                mcp_config_path,
                "-p",
                full_prompt,
            ]

            env = {**os.environ, "ANTHROPIC_API_KEY": self._api_key}

            logger.debug(
                "ClaudeCodeWorker[%s] spawning claude CLI, timeout=%ds",
                self.worker_id[:8],
                self.timeout_s,
            )

            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    self._process.communicate(),
                    timeout=self.timeout_s,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "ClaudeCodeWorker[%s] timed out after %ds — killing process",
                    self.worker_id[:8],
                    self.timeout_s,
                )
                await self.teardown()
                return f"Task timed out after {self.timeout_s}s."

            stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
            stderr = stderr_bytes.decode("utf-8", errors="replace").strip()

            return_code = self._process.returncode
            self._process = None

            if return_code != 0:
                logger.warning(
                    "ClaudeCodeWorker[%s] claude exited with code %d, stderr=%r",
                    self.worker_id[:8],
                    return_code,
                    stderr[:500],
                )
                if stdout:
                    return stdout
                return f"claude CLI exited with code {return_code}: {stderr[:500]}"

            if stderr:
                logger.debug(
                    "ClaudeCodeWorker[%s] stderr: %s", self.worker_id[:8], stderr[:500]
                )

            return stdout

        finally:
            # Clean up temp config file
            try:
                Path(mcp_config_path).unlink(missing_ok=True)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    async def teardown(self) -> None:
        """Kill the subprocess if it is still running."""
        if self._process is not None:
            try:
                self._process.kill()
                await asyncio.shield(self._process.wait())
            except (ProcessLookupError, OSError):
                pass
            finally:
                self._process = None
