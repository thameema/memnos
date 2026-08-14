"""Tommy configuration loader — reads ~/.memnos/agents/tommy/tommy.conf (KEY=VALUE)."""
from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_BUNDLED_DEFAULT = Path(__file__).parent / "tommy.conf.default"
_USER_CONF = Path.home() / ".memnos" / "agents" / "tommy" / "tommy.conf"


def _expand(val: str) -> str:
    return os.path.expandvars(os.path.expanduser(val))


def _parse_kv(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        result[k.strip()] = v.strip().strip('"').strip("'")
    return result


@dataclass
class ProjectEntry:
    key: str          # short key, e.g. "myapp"
    name: str         # display name, e.g. "MyApp"
    jira_project: str # JIRA key, e.g. "APP"
    git_root: Path    # absolute path


def _parse_projects(raw: str) -> list[ProjectEntry]:
    """Parse PROJECTS=key:NAME:JIRA:~/git/repo,..."""
    entries = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(":", 3)
        if len(parts) != 4:
            continue
        key, name, jira, git_root = parts
        entries.append(ProjectEntry(
            key=key.strip(),
            name=name.strip(),
            jira_project=jira.strip(),
            git_root=Path(_expand(git_root.strip())),
        ))
    return entries


@dataclass
class TommyConfig:
    tommy_user: str = "developer"
    org: str = "myorg"
    prompts_dir: Path = field(default_factory=lambda: Path(__file__).parent / "prompts")
    tommy_ns: str = "user:tommy"
    default_ns: str = "org:engineering"
    default_model: str = "claude-sonnet-4-5"
    projects: list[ProjectEntry] = field(default_factory=list)
    smart_routing: bool = True
    mcp_introspect: bool = False
    harness: str = "claude"
    memnos_url: str = "http://127.0.0.1:8900"
    memnos_token: Optional[str] = None

    # derived
    _raw: dict[str, str] = field(default_factory=dict, repr=False)

    @classmethod
    def load(cls, conf_path: Optional[Path] = None) -> "TommyConfig":
        """Load config, merging bundled defaults → user conf → explicit path."""
        raw: dict[str, str] = {}

        # 1. bundled defaults
        if _BUNDLED_DEFAULT.exists():
            raw.update(_parse_kv(_BUNDLED_DEFAULT))

        # 2. user config (~/.memnos/agents/tommy/tommy.conf)
        if _USER_CONF.exists():
            raw.update(_parse_kv(_USER_CONF))

        # 3. explicit override
        if conf_path and Path(conf_path).exists():
            raw.update(_parse_kv(Path(conf_path)))

        # 4. env overrides
        if "MEMNOS_URL" in os.environ:
            raw["MEMNOS_URL"] = os.environ["MEMNOS_URL"]
        if "MEMNOS_SECRET_KEY" in os.environ:
            raw["MEMNOS_TOKEN"] = os.environ["MEMNOS_SECRET_KEY"]

        cfg = cls()
        cfg._raw = raw

        if "TOMMY_USER" in raw:
            cfg.tommy_user = raw["TOMMY_USER"]
        if "ORG" in raw:
            cfg.org = raw["ORG"]
        if "PROMPTS_DIR" in raw:
            cfg.prompts_dir = Path(_expand(raw["PROMPTS_DIR"]))
        if "TOMMY_NS" in raw:
            cfg.tommy_ns = raw["TOMMY_NS"]
        if "DEFAULT_NS" in raw:
            cfg.default_ns = raw["DEFAULT_NS"]
        if "DEFAULT_MODEL" in raw:
            cfg.default_model = raw["DEFAULT_MODEL"]
        if "PROJECTS" in raw:
            cfg.projects = _parse_projects(raw["PROJECTS"])
        if "SMART_ROUTING" in raw:
            cfg.smart_routing = raw["SMART_ROUTING"].lower() in ("on", "true", "1", "yes")
        if "MCP_INTROSPECT" in raw:
            cfg.mcp_introspect = raw["MCP_INTROSPECT"].lower() in ("on", "true", "1", "yes")
        if "HARNESS" in raw:
            cfg.harness = raw["HARNESS"]
        if "MEMNOS_URL" in raw:
            cfg.memnos_url = raw["MEMNOS_URL"]
        if "MEMNOS_TOKEN" in raw:
            cfg.memnos_token = raw["MEMNOS_TOKEN"]

        return cfg

    def project_by_key(self, key: str) -> Optional[ProjectEntry]:
        for p in self.projects:
            if p.key == key:
                return p
        return None
