"""
engram_mcp.tools.vault — MCP tool handlers for the secrets vault.

Tools
-----
vault_secret_set    — store / replace a secret
vault_secret_get    — retrieve decrypted value
vault_secret_list   — list metadata (no values)
vault_secret_rotate — re-encrypt with new value
vault_audit         — read audit log
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)


async def handle_secret_set(
    client,
    key_name: str,
    value: str,
    namespace: str,
    secret_type: str = "api_key",
    description: str = "",
    tags: list[str] | None = None,
) -> str:
    """Store or replace a secret in the vault."""
    result = await client.secret_set(
        key_name=key_name,
        value=value,
        namespace=namespace,
        secret_type=secret_type,
        description=description,
        created_by="mcp",
        tags=tags or [],
    )
    return json.dumps(result)


async def handle_secret_get(
    client,
    key_name: str,
    namespace: str,
) -> str:
    """Retrieve the decrypted value of a secret."""
    try:
        value = await client.secret_get(
            key_name=key_name,
            namespace=namespace,
            accessed_by="mcp",
        )
        return json.dumps({"key_name": key_name, "namespace": namespace, "value": value})
    except KeyError as exc:
        return json.dumps({"error": str(exc)})


async def handle_secret_list(
    client,
    namespace: str,
) -> str:
    """List vault secret metadata (no plaintext values)."""
    secrets = await client.secret_list(namespace=namespace, accessed_by="mcp")
    return json.dumps(secrets)


async def handle_secret_rotate(
    client,
    key_name: str,
    new_value: str,
    namespace: str,
) -> str:
    """Re-encrypt a secret with a new value."""
    result = await client.secret_rotate(
        key_name=key_name,
        new_value=new_value,
        namespace=namespace,
        accessed_by="mcp",
    )
    return json.dumps(result)


async def handle_vault_audit(
    client,
    namespace: str,
    limit: int = 100,
) -> str:
    """Return vault audit log entries."""
    logs = await client.secret_audit(namespace=namespace, limit=limit)
    return json.dumps(logs, default=str)
