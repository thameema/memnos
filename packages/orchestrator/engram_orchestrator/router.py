"""
engram_orchestrator.router — Agent matching via semantic similarity over YAML agent definitions.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_MATCH_THRESHOLD = 0.82


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two equal-length float vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


class AgentRouter:
    """
    Loads agent YAML definitions, embeds their descriptions, and matches tasks
    to the most relevant agent via cosine similarity.
    """

    def __init__(self, agents_dir: str, engram_client: Any) -> None:
        self._agents_dir = Path(agents_dir)
        self._engram_client = engram_client
        # List of (agent_dict, description_embedding)
        self._agent_embeddings: list[tuple[dict[str, Any], list[float]]] = []
        # name → agent_dict cache
        self._agents_by_name: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    async def init(self) -> None:
        """
        Load all YAML files from agents_dir, embed their descriptions,
        and store for matching.
        """
        self._agent_embeddings.clear()
        self._agents_by_name.clear()

        if not self._agents_dir.exists():
            logger.warning(
                "AgentRouter: agents_dir %s does not exist — no agents loaded",
                self._agents_dir,
            )
            return

        yaml_files = list(self._agents_dir.glob("**/*.yaml")) + list(
            self._agents_dir.glob("**/*.yml")
        )

        if not yaml_files:
            logger.info("AgentRouter: no YAML agent files found in %s", self._agents_dir)
            return

        agents: list[dict[str, Any]] = []
        for yaml_file in yaml_files:
            try:
                with yaml_file.open("r", encoding="utf-8") as fh:
                    data = yaml.safe_load(fh)
                if isinstance(data, dict) and "name" in data:
                    agents.append(data)
                    logger.debug("AgentRouter: loaded agent %r from %s", data["name"], yaml_file)
            except Exception as exc:
                logger.warning("AgentRouter: failed to load %s — %s", yaml_file, exc)

        if not agents:
            return

        # Build description texts for embedding
        descriptions: list[str] = []
        for agent in agents:
            desc = _agent_description_text(agent)
            descriptions.append(desc)
            name = str(agent.get("name", ""))
            if name:
                self._agents_by_name[name] = agent

        # Embed all descriptions in one batch if possible
        try:
            embedder = getattr(self._engram_client, "embedder", None)
            if embedder is not None:
                embeddings = await embedder.embed_batch(descriptions)
            else:
                # Fall back to individual embed calls via search (best-effort)
                embeddings = []
                for desc in descriptions:
                    try:
                        results = await self._engram_client.search(
                            desc, "__agent_router__", top_k=1, mode="vector"
                        )
                    except Exception:
                        pass
                    embeddings.append([])  # no embedding available without embedder
        except Exception as exc:
            logger.warning("AgentRouter: embedding failed — %s", exc)
            embeddings = [[] for _ in agents]

        for agent, emb in zip(agents, embeddings):
            self._agent_embeddings.append((agent, emb))

        logger.info("AgentRouter: loaded %d agents", len(agents))

    # ------------------------------------------------------------------
    # Matching
    # ------------------------------------------------------------------

    async def match(self, task: str, namespace: str) -> dict[str, Any] | None:
        """
        Find the best-matching agent for `task`.

        Returns the agent definition dict if similarity > 0.82, else None.
        """
        if not self._agent_embeddings:
            return None

        # Get embedding for the task
        try:
            embedder = getattr(self._engram_client, "embedder", None)
            if embedder is not None:
                task_embedding = await embedder.embed(task)
            else:
                return None
        except Exception as exc:
            logger.debug("AgentRouter.match: embedding task failed — %s", exc)
            return None

        best_score = 0.0
        best_agent: dict[str, Any] | None = None

        for agent, agent_emb in self._agent_embeddings:
            if not agent_emb:
                continue
            try:
                score = _cosine_similarity(task_embedding, agent_emb)
            except Exception:
                continue
            if score > best_score:
                best_score = score
                best_agent = agent

        if best_score >= _MATCH_THRESHOLD and best_agent is not None:
            logger.debug(
                "AgentRouter.match: matched %r (score=%.3f)",
                best_agent.get("name"),
                best_score,
            )
            return best_agent

        return None

    # ------------------------------------------------------------------
    # Direct load by name
    # ------------------------------------------------------------------

    def load_agent(self, name: str) -> dict[str, Any] | None:
        """Load a specific agent by name."""
        if name in self._agents_by_name:
            return self._agents_by_name[name]

        # Try to find it on disk if not yet loaded
        if not self._agents_dir.exists():
            return None

        for yaml_file in self._agents_dir.glob("**/*.yaml"):
            try:
                with yaml_file.open("r", encoding="utf-8") as fh:
                    data = yaml.safe_load(fh)
                if isinstance(data, dict) and data.get("name") == name:
                    self._agents_by_name[name] = data
                    return data
            except Exception:
                pass

        for yaml_file in self._agents_dir.glob("**/*.yml"):
            try:
                with yaml_file.open("r", encoding="utf-8") as fh:
                    data = yaml.safe_load(fh)
                if isinstance(data, dict) and data.get("name") == name:
                    self._agents_by_name[name] = data
                    return data
            except Exception:
                pass

        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _agent_description_text(agent: dict[str, Any]) -> str:
    """
    Build a rich description string from an agent YAML for embedding.

    Combines name, description, and any role/skills fields if present.
    """
    parts: list[str] = []
    for field in ("name", "description", "role", "skills", "capabilities"):
        val = agent.get(field)
        if val is None:
            continue
        if isinstance(val, list):
            parts.append(", ".join(str(v) for v in val))
        else:
            parts.append(str(val))
    return " ".join(parts) if parts else str(agent)
