"""
engram_api.routers.agents — Agent registry endpoints.

Endpoints
---------
GET  /agents/          — list all available agents
GET  /agents/{name}    — get a specific agent by name
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from engram_api.auth import require_api_key

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/agents", tags=["agents"])


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class AgentResponse(BaseModel):
    name: str
    version: str = "1.0"
    description: str = ""
    model: str = ""
    temperature: float | None = None
    max_tokens: int | None = None
    tools: list[str] = []
    use_critic: bool = False
    critic_model: str | None = None
    timeout_s: int = 300
    system_prompt_preview: str = ""


def _yaml_to_agent(data: dict) -> AgentResponse:
    system_prompt = data.get("system_prompt", "")
    preview = (system_prompt[:200] + "…") if len(system_prompt) > 200 else system_prompt
    return AgentResponse(
        name=data.get("name", ""),
        version=str(data.get("version", "1.0")),
        description=data.get("description", ""),
        model=data.get("model", ""),
        temperature=data.get("temperature"),
        max_tokens=data.get("max_tokens"),
        tools=data.get("tools", []),
        use_critic=bool(data.get("use_critic", False)),
        critic_model=data.get("critic_model"),
        timeout_s=int(data.get("timeout_s", 300)),
        system_prompt_preview=preview,
    )


def _load_agents(agents_dir: str) -> list[AgentResponse]:
    """Load all YAML agent definitions from *agents_dir* (recursive)."""
    agents_path = Path(agents_dir)
    if not agents_path.exists():
        return []

    agents: list[AgentResponse] = []
    for yaml_file in sorted(agents_path.rglob("*.yaml")) + sorted(agents_path.rglob("*.yml")):
        try:
            data = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "name" in data:
                agents.append(_yaml_to_agent(data))
        except Exception as exc:
            logger.warning("Failed to load agent from %s: %s", yaml_file, exc)

    return agents


def _get_agents_dir() -> str:
    return os.environ.get("ENGRAM_AGENTS_DIR", "agents")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/", response_model=list[AgentResponse])
async def list_agents(
    _user_id: str = Depends(require_api_key),
):
    """Return all available agents in the agents directory."""
    agents_dir = _get_agents_dir()
    agents = _load_agents(agents_dir)
    return agents


@router.get("/{name}", response_model=AgentResponse)
async def get_agent(
    name: str,
    _user_id: str = Depends(require_api_key),
):
    """Return a specific agent by name."""
    agents_dir = _get_agents_dir()
    for agent in _load_agents(agents_dir):
        if agent.name == name:
            return agent
    raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
