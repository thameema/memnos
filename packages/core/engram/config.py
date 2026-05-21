"""
engram.config — Configuration loading via YAML + env-var expansion.

Load order:
  1. Read engram.yaml (or the path passed to EngramConfig.from_yaml)
  2. Expand ${VAR} references against os.environ
  3. Construct Pydantic models — extra env overrides via ENGRAM__ prefix are NOT
     applied here (keep it simple; use ${VAR} in the YAML instead).
"""

from __future__ import annotations

import os
import re
import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_ENV_RE = re.compile(r"\$\{([^}]+)\}")


def _expand_env(value: Any) -> Any:
    """Recursively expand ${VAR} references in strings inside dicts/lists."""
    if isinstance(value, str):
        def _replace(m: re.Match) -> str:
            var = m.group(1)
            result = os.environ.get(var, "")
            if not result:
                logger.warning("Environment variable %r referenced in config but not set", var)
            return result
        return _ENV_RE.sub(_replace, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    return value


# ---------------------------------------------------------------------------
# Sub-configs
# ---------------------------------------------------------------------------

class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    mcp_port: int = 8765
    api_port: int = 8766
    log_level: str = "INFO"


class Neo4jConfig(BaseModel):
    uri: str = "bolt://localhost:7687"
    username: str = "neo4j"
    password: str = ""
    database: str = "neo4j"


class QdrantConfig(BaseModel):
    host: str = "localhost"
    port: int = 6333
    collection: str = "engram_memories"


class EmbeddingsConfig(BaseModel):
    provider: str = "openai"  # "openai" | "local"
    model: str = "text-embedding-3-small"
    api_key: str = ""


class ApiRuntimeConfig(BaseModel):
    provider: str = "anthropic"
    model: str = "claude-sonnet-4-6"
    api_key: str = ""


class OpenRouterConfig(BaseModel):
    model: str = "anthropic/claude-sonnet-4-6"
    api_key: str = ""


class RuntimeConfig(BaseModel):
    default: str = "api"  # "api" | "claude-code" | "openrouter"
    max_concurrent_workers: int = 5
    worker_timeout_s: int = 300
    api: ApiRuntimeConfig = Field(default_factory=ApiRuntimeConfig)
    openrouter: OpenRouterConfig = Field(default_factory=OpenRouterConfig)


class NamespaceDefinition(BaseModel):
    owners: list[str] = Field(default_factory=list)
    readers: list[str] = Field(default_factory=list)
    writers: list[str] = Field(default_factory=list)


class NamespaceConfig(BaseModel):
    default: str = "personal:default"
    definitions: dict[str, NamespaceDefinition] = Field(default_factory=dict)

    @classmethod
    def _parse_definitions(cls, raw: dict) -> dict[str, NamespaceDefinition]:
        result: dict[str, NamespaceDefinition] = {}
        for name, defn in raw.items():
            if isinstance(defn, dict):
                result[name] = NamespaceDefinition(**defn)
            else:
                result[name] = NamespaceDefinition()
        return result


class EpisodicConfig(BaseModel):
    enabled: bool = True
    retention_days: int = 365


class ReflectionConfig(BaseModel):
    enabled: bool = True
    schedule: str = "0 2 * * *"
    trigger_on_correction: bool = True
    min_episodes_per_run: int = 5
    lookback_days: int = 7
    model: str = "claude-haiku-4-5-20251001"


class SkillExtractionConfig(BaseModel):
    enabled: bool = True
    quality_threshold: float = 0.8
    similarity_threshold: float = 0.92


class HeuristicDecayConfig(BaseModel):
    enabled: bool = True
    schedule: str = "0 3 * * 0"
    inactive_days_before_decay: int = 30
    decay_rate: float = 0.9


class QualityRoutingConfig(BaseModel):
    enabled: bool = True
    min_samples: int = 10
    quality_threshold: float = 0.6


class LearningConfig(BaseModel):
    enabled: bool = True
    episodic: EpisodicConfig = Field(default_factory=EpisodicConfig)
    reflection: ReflectionConfig = Field(default_factory=ReflectionConfig)
    skill_extraction: SkillExtractionConfig = Field(default_factory=SkillExtractionConfig)
    heuristic_decay: HeuristicDecayConfig = Field(default_factory=HeuristicDecayConfig)
    quality_routing: QualityRoutingConfig = Field(default_factory=QualityRoutingConfig)


# ---------------------------------------------------------------------------
# Top-level config
# ---------------------------------------------------------------------------

class EngramConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    neo4j: Neo4jConfig = Field(default_factory=Neo4jConfig)
    qdrant: QdrantConfig = Field(default_factory=QdrantConfig)
    embeddings: EmbeddingsConfig = Field(default_factory=EmbeddingsConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    namespaces: NamespaceConfig = Field(default_factory=NamespaceConfig)
    learning: LearningConfig = Field(default_factory=LearningConfig)

    # ---------------------------------------------------------------------------
    # Factory
    # ---------------------------------------------------------------------------

    @classmethod
    def from_yaml(cls, path: str | Path = "engram.yaml") -> "EngramConfig":
        """Load configuration from a YAML file with ${VAR} env-var expansion.

        Parameters
        ----------
        path:
            Path to the YAML configuration file.  Defaults to ``engram.yaml``
            in the current working directory.

        Returns
        -------
        EngramConfig
            Fully-populated configuration object.
        """
        config_path = Path(path)
        if not config_path.exists():
            logger.warning(
                "Config file %s not found — using defaults. "
                "Copy engram.yaml.example to engram.yaml to customise.",
                config_path,
            )
            return cls()

        with config_path.open("r", encoding="utf-8") as fh:
            raw: dict = yaml.safe_load(fh) or {}

        raw = _expand_env(raw)

        # Pull out each section, ignoring unknown top-level keys (e.g. auth, gateway)
        kwargs: dict[str, Any] = {}

        if "server" in raw:
            kwargs["server"] = ServerConfig(**raw["server"])

        if "neo4j" in raw:
            kwargs["neo4j"] = Neo4jConfig(**raw["neo4j"])

        if "qdrant" in raw:
            kwargs["qdrant"] = QdrantConfig(**raw["qdrant"])

        if "embeddings" in raw:
            kwargs["embeddings"] = EmbeddingsConfig(**raw["embeddings"])

        if "runtime" in raw:
            rt = dict(raw["runtime"])
            api_raw = rt.pop("api", {})
            or_raw = rt.pop("openrouter", {})
            kwargs["runtime"] = RuntimeConfig(
                **rt,
                api=ApiRuntimeConfig(**api_raw) if api_raw else ApiRuntimeConfig(),
                openrouter=OpenRouterConfig(**or_raw) if or_raw else OpenRouterConfig(),
            )

        if "namespaces" in raw:
            ns_raw = dict(raw["namespaces"])
            defs_raw = ns_raw.pop("definitions", {})
            parsed_defs = NamespaceConfig._parse_definitions(defs_raw)
            kwargs["namespaces"] = NamespaceConfig(definitions=parsed_defs, **ns_raw)

        if "learning" in raw:
            lr = dict(raw["learning"])
            episodic_raw = lr.pop("episodic", {})
            reflection_raw = lr.pop("reflection", {})
            skill_raw = lr.pop("skill_extraction", {})
            decay_raw = lr.pop("heuristic_decay", {})
            routing_raw = lr.pop("quality_routing", {})
            # Drop feedback key (not modelled in LearningConfig)
            lr.pop("feedback", None)
            kwargs["learning"] = LearningConfig(
                **lr,
                episodic=EpisodicConfig(**episodic_raw) if episodic_raw else EpisodicConfig(),
                reflection=ReflectionConfig(**reflection_raw) if reflection_raw else ReflectionConfig(),
                skill_extraction=SkillExtractionConfig(**skill_raw) if skill_raw else SkillExtractionConfig(),
                heuristic_decay=HeuristicDecayConfig(**decay_raw) if decay_raw else HeuristicDecayConfig(),
                quality_routing=QualityRoutingConfig(**routing_raw) if routing_raw else QualityRoutingConfig(),
            )

        logger.debug("Loaded engram config from %s", config_path)
        return cls(**kwargs)
