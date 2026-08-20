"""
Effective config resolution — the single source of truth `tommy config show`
prints, and the source `tommy generate` reads from to render harness adapter
blocks.

Precedence (lowest to highest; documented in the README and in issue #113):

    1. tommy.conf   — installed/global INI defaults (config.py's TommyConfig,
                       itself already layered: bundled tommy.conf.default →
                       ~/.memnos/agents/tommy/tommy.conf → an explicit --conf)
    2. tommy.yaml   — project config, committed to the repo. Only fields the
                       file actually sets participate; anything absent falls
                       through to the tommy.conf value untouched.
    3. env vars     — highest precedence, for one-off local overrides without
                       editing either file. See the ENV_* constants below for names.

Every resolved field records which of the three layers it came from, so
`tommy config show` can answer "why is this the value it is" — the whole
point of this module (see issue #113's support-gap framing: config
resolution confusion is the same class of problem as the stale-version-banner
bug, and deserves the same kind of debuggable single source of truth).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .config import TommyConfig
from .project_config import TommyYamlConfig, find_tommy_yaml, load_tommy_yaml

# Env var names for the highest-precedence layer. Deliberately NOT reusing
# TOMMY_NS / TOMMY_DEFAULT_NS — those are runtime env vars Tommy *exports* to
# the harness subprocess it launches (see cli.py._launch_harness); reusing
# the same names here as *inputs* this module reads would make one name mean
# two different things depending on which side of the process boundary you're
# looking from. TOMMY_CFG_* is unambiguous: it's always an input to config
# resolution, never an output.
ENV_DEFAULT_MODEL = "TOMMY_CFG_DEFAULT_MODEL"
ENV_HARNESS = "TOMMY_CFG_HARNESS"
ENV_SMART_ROUTING = "TOMMY_CFG_SMART_ROUTING"
ENV_MCP_INTROSPECT = "TOMMY_CFG_MCP_INTROSPECT"
ENV_SKIP_PERMISSIONS = "TOMMY_CFG_SKIP_PERMISSIONS"
ENV_NAMESPACE = "TOMMY_CFG_NAMESPACE"
ENV_PROJECT_NAME = "TOMMY_CFG_PROJECT_NAME"
ENV_PROJECT_KEY = "TOMMY_CFG_PROJECT_KEY"
ENV_PROJECT_GIT_ROOT = "TOMMY_CFG_PROJECT_GIT_ROOT"
ENV_DESIGN_DOCS = "TOMMY_CFG_DESIGN_DOCS"  # comma-separated glob list
ENV_CORPUS_GATE = "TOMMY_CFG_CORPUS_GATE"
ENV_AUTO_INGEST = "TOMMY_CFG_AUTO_INGEST"
ENV_MERGE_GATE = "TOMMY_CFG_MERGE_GATE"
ENV_WAVE_LIMIT = "TOMMY_CFG_WAVE_LIMIT"

# Built-in fallbacks used when neither tommy.conf, tommy.yaml, nor env sets a
# value and there's no TommyConfig equivalent to fall back to (project.*,
# design_docs, corpus.*, merge_gate, wave_limit — all new in this issue).
# wave_limit's default of 4 matches core.md's existing prompt-only "wave-based
# dispatch (max 4/turn)" text, so formalizing it into config doesn't silently
# change today's behavior for anyone who hasn't opted into tommy.yaml yet.
DEFAULT_WAVE_LIMIT = 4
DEFAULT_MERGE_GATE = False
DEFAULT_CORPUS_GATE = False
DEFAULT_AUTO_INGEST = False

_TRUE_STRINGS = ("on", "true", "1", "yes")


def _env_bool(raw: str) -> bool:
    return raw.strip().lower() in _TRUE_STRINGS


@dataclass
class ResolvedField:
    value: Any
    source: str  # "tommy.conf" | "tommy.yaml" | "env" | "default"


@dataclass
class EffectiveConfig:
    """Fully-resolved Tommy config with per-field provenance."""

    fields: dict[str, ResolvedField] = field(default_factory=dict)
    tommy_yaml_path: Optional[Path] = None
    tommy_conf_used: bool = False

    def value(self, name: str) -> Any:
        return self.fields[name].value

    def source(self, name: str) -> str:
        return self.fields[name].source

    def as_dict(self) -> dict[str, Any]:
        return {name: rf.value for name, rf in self.fields.items()}

    def as_provenance_dict(self) -> dict[str, dict[str, Any]]:
        return {
            name: {"value": rf.value, "source": rf.source}
            for name, rf in self.fields.items()
        }


def _set(
    resolved: dict[str, ResolvedField],
    name: str,
    base_value: Any,
    yaml_value: Any,
    env_name: Optional[str],
    env,
    *,
    env_cast=None,
    base_source: str = "tommy.conf",
) -> None:
    """Layer base (tommy.conf, or a built-in default when `base_source`
    says so) -> tommy.yaml -> env, recording which layer won."""
    value = base_value
    source = base_source
    if yaml_value is not None:
        value = yaml_value
        source = "tommy.yaml"
    if env_name and env_name in env:
        raw = env[env_name]
        value = env_cast(raw) if env_cast else raw
        source = "env"
    resolved[name] = ResolvedField(value=value, source=source)


def resolve_effective_config(
    *,
    conf_path: Optional[Path] = None,
    tommy_yaml_path: Optional[Path] = None,
    project_root: Optional[Path] = None,
    env: Optional[dict[str, str]] = None,
) -> EffectiveConfig:
    """Resolve the fully-layered config: tommy.conf -> tommy.yaml -> env.

    `tommy_yaml_path`, if given, is used directly (must exist). Otherwise
    tommy.yaml is discovered by walking up from `project_root` (default: CWD).
    A missing tommy.yaml is not an error — every yaml-sourced field simply
    falls back to its tommy.conf / built-in default.
    """
    env = os.environ if env is None else env

    base = TommyConfig.load(conf_path=conf_path)

    yaml_path: Optional[Path] = tommy_yaml_path
    if yaml_path is None:
        yaml_path = find_tommy_yaml(project_root)
    yaml_cfg: Optional[TommyYamlConfig] = None
    if yaml_path is not None:
        yaml_cfg = load_tommy_yaml(yaml_path)

    resolved: dict[str, ResolvedField] = {}

    # --- agent/harness dispatch fields (mirror TommyConfig) -----------------
    _set(resolved, "default_model", base.default_model,
         yaml_cfg.agents.default_model if yaml_cfg else None,
         ENV_DEFAULT_MODEL, env)
    _set(resolved, "harness", base.harness,
         yaml_cfg.agents.harness if yaml_cfg else None,
         ENV_HARNESS, env)
    _set(resolved, "smart_routing", base.smart_routing,
         yaml_cfg.agents.smart_routing if yaml_cfg else None,
         ENV_SMART_ROUTING, env, env_cast=_env_bool)
    _set(resolved, "mcp_introspect", base.mcp_introspect,
         yaml_cfg.agents.mcp_introspect if yaml_cfg else None,
         ENV_MCP_INTROSPECT, env, env_cast=_env_bool)
    _set(resolved, "skip_permissions", base.skip_permissions,
         yaml_cfg.agents.skip_permissions if yaml_cfg else None,
         ENV_SKIP_PERMISSIONS, env, env_cast=_env_bool)

    # --- memnos namespace -----------------------------------------------
    yaml_namespace = yaml_cfg.memnos.namespace if (yaml_cfg and yaml_cfg.memnos.namespace) else None
    _set(resolved, "namespace", base.default_ns, yaml_namespace, ENV_NAMESPACE, env)

    # --- fields with no tommy.conf equivalent (new in tommy.yaml) -------
    yaml_project_name = yaml_cfg.project.name if (yaml_cfg and yaml_cfg.project.name) else None
    _set(resolved, "project_name", None, yaml_project_name, ENV_PROJECT_NAME, env,
         base_source="default")

    yaml_project_key = yaml_cfg.project.key if (yaml_cfg and yaml_cfg.project.key) else None
    _set(resolved, "project_key", None, yaml_project_key, ENV_PROJECT_KEY, env,
         base_source="default")

    yaml_git_root = yaml_cfg.project.git_root if (yaml_cfg and yaml_cfg.project.git_root) else None
    default_git_root = str(yaml_path.parent) if yaml_path else None
    _set(resolved, "project_git_root", default_git_root, yaml_git_root, ENV_PROJECT_GIT_ROOT, env,
         base_source="default")

    yaml_design_docs = yaml_cfg.design_docs if (yaml_cfg and yaml_cfg.design_docs) else None
    env_design_docs = None
    if ENV_DESIGN_DOCS in env:
        env_design_docs = [g.strip() for g in env[ENV_DESIGN_DOCS].split(",") if g.strip()]
    design_docs_value = [] if yaml_design_docs is None else yaml_design_docs
    design_docs_source = "tommy.yaml" if yaml_design_docs else "default"
    if env_design_docs is not None:
        design_docs_value, design_docs_source = env_design_docs, "env"
    resolved["design_docs"] = ResolvedField(value=design_docs_value, source=design_docs_source)

    _set(resolved, "corpus_gate", DEFAULT_CORPUS_GATE,
         yaml_cfg.corpus.corpus_gate if yaml_cfg else None,
         ENV_CORPUS_GATE, env, env_cast=_env_bool, base_source="default")
    _set(resolved, "auto_ingest", DEFAULT_AUTO_INGEST,
         yaml_cfg.corpus.auto_ingest if yaml_cfg else None,
         ENV_AUTO_INGEST, env, env_cast=_env_bool, base_source="default")
    _set(resolved, "merge_gate", DEFAULT_MERGE_GATE,
         yaml_cfg.merge_gate if yaml_cfg else None,
         ENV_MERGE_GATE, env, env_cast=_env_bool, base_source="default")
    _set(resolved, "wave_limit", DEFAULT_WAVE_LIMIT,
         yaml_cfg.wave_limit if yaml_cfg else None,
         ENV_WAVE_LIMIT, env, env_cast=int, base_source="default")

    return EffectiveConfig(
        fields=resolved,
        tommy_yaml_path=yaml_path,
        tommy_conf_used=True,
    )
