"""
tommy.yaml — the project-committable, team-shared half of Tommy's config.

Where tommy.conf (config.py) is a per-user INI file installed at
``~/.memnos/agents/tommy/tommy.conf`` and never checked into a repo,
tommy.yaml lives at a project's root, is meant to be committed alongside the
code it governs, and carries only policy — never secrets or credentials.
Nothing in this schema accepts a token, key, password, or URL with embedded
auth; if a field ever needs one, it belongs in tommy.conf or an environment
variable instead, never in this file.

Schema (all sections optional except ``tommy.version``):

    tommy:
      version: 1
    project:
      name: string        # free-form/informational only
      key: string          # free-form/informational only
      git_root: string     # optional, defaults to the repo root tommy.yaml lives in
    memnos:
      namespace: string
    design_docs:
      - glob patterns for hand-authored ADRs/design docs
    corpus:
      corpus_gate: bool    # gate dispatch on corpus_check before proceeding (feeds #109)
      auto_ingest: bool    # auto-ingest design_docs matches into the corpus (feeds #109)
    agents:
      default_model: string
      harness: string
      smart_routing: bool
      mcp_introspect: bool
      skip_permissions: bool
    merge_gate: bool        # formalizes core.md's wave-based dispatch concept
    wave_limit: int

Deliberately absent (see issue #113 — these are exclusions, not omissions):
  - `platform:` — no GitLab/GitHub/Azure-specific integration logic. Tommy
    stays platform-agnostic.
  - scheduler ownership — Tommy never owns cron/launchd config.
  - `peer_approver:` — considered and cut; undesigned semantics, needs its
    own issue if ever wanted.
  - `harness:` at the top level — which coding harness a person runs is
    local/machine-specific, not a team-wide committed decision. (`agents.harness`
    is a *default suggestion* mirroring tommy.conf's existing HARNESS field,
    not a mandate — same precedence rules as everything else in this file.)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

SUPPORTED_VERSIONS = (1,)

# Keys that are explicitly excluded from this schema by design (see issue #113).
# Presence of any of these at the top level is a hard error, not a silent ignore —
# a team member typing `platform:` into a committed file should find out
# immediately that it does nothing, not discover it three weeks later.
_EXCLUDED_TOP_LEVEL_KEYS = {
    "platform": (
        "no platform-specific integration logic (GitLab/GitHub/Azure clients) belongs "
        "in tommy.yaml — Tommy stays platform-agnostic; your own environment/tooling "
        "dictates that, not Tommy's config."
    ),
    "peer_approver": (
        "peer_approver was considered and cut when this schema was designed — it has "
        "no implementation anywhere and its semantics are undesigned. File a new issue "
        "if you want a real peer-approval gate."
    ),
    "harness": (
        "top-level `harness:` is not part of this schema on purpose — which coding "
        "harness a person runs is local/machine-specific, not a team-wide committed "
        "decision. Use `agents.harness` instead (a default suggestion, same precedence "
        "rules as every other field)."
    ),
    "scheduler": (
        "Tommy does not own scheduling — set up your own cron/launchd job if you want "
        "scheduled runs."
    ),
}

_KNOWN_TOP_LEVEL_KEYS = {
    "tommy",
    "project",
    "memnos",
    "design_docs",
    "corpus",
    "agents",
    "merge_gate",
    "wave_limit",
}


class TommyYamlError(ValueError):
    """Raised for a structurally or semantically invalid tommy.yaml."""


def _require_type(value: Any, expected: type, path: str) -> None:
    if expected is int:
        # bool is a subclass of int in Python — reject it explicitly so a
        # stray `wave_limit: true` doesn't silently pass as an integer.
        if isinstance(value, bool) or not isinstance(value, int):
            raise TommyYamlError(f"{path}: expected an integer, got {value!r}")
        return
    if expected is bool:
        if not isinstance(value, bool):
            raise TommyYamlError(f"{path}: expected true/false, got {value!r}")
        return
    if not isinstance(value, expected):
        raise TommyYamlError(f"{path}: expected {expected.__name__}, got {value!r}")


@dataclass
class ProjectBlock:
    name: str = ""
    key: str = ""
    git_root: Optional[str] = None


@dataclass
class MemnosBlock:
    namespace: str = ""


@dataclass
class CorpusBlock:
    corpus_gate: Optional[bool] = None
    auto_ingest: Optional[bool] = None


@dataclass
class AgentsBlock:
    """Mirrors the subset of tommy.conf's TommyConfig fields that govern
    agent/harness dispatch behavior (see config.py). Every field is optional —
    only fields actually present in tommy.yaml participate in precedence
    resolution; unset fields fall through to tommy.conf's value."""
    default_model: Optional[str] = None
    harness: Optional[str] = None
    smart_routing: Optional[bool] = None
    mcp_introspect: Optional[bool] = None
    skip_permissions: Optional[bool] = None


@dataclass
class TommyYamlConfig:
    version: int = 1
    project: ProjectBlock = field(default_factory=ProjectBlock)
    memnos: MemnosBlock = field(default_factory=MemnosBlock)
    design_docs: list[str] = field(default_factory=list)
    corpus: CorpusBlock = field(default_factory=CorpusBlock)
    agents: AgentsBlock = field(default_factory=AgentsBlock)
    merge_gate: Optional[bool] = None
    wave_limit: Optional[int] = None

    # Not part of the schema — bookkeeping for diagnostics / `tommy config show`.
    source_path: Optional[Path] = field(default=None, repr=False)


def parse_tommy_yaml(text: str, *, source: str = "<string>") -> TommyYamlConfig:
    """Parse tommy.yaml text into a validated TommyYamlConfig. Raises
    TommyYamlError on anything structurally or semantically wrong."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise TommyYamlError(f"{source}: invalid YAML — {exc}") from exc

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise TommyYamlError(f"{source}: top level must be a mapping, got {type(raw).__name__}")

    for bad_key, why in _EXCLUDED_TOP_LEVEL_KEYS.items():
        if bad_key in raw:
            raise TommyYamlError(f"{source}: `{bad_key}:` is not a valid tommy.yaml field — {why}")

    unknown = set(raw) - _KNOWN_TOP_LEVEL_KEYS
    if unknown:
        raise TommyYamlError(
            f"{source}: unknown top-level key(s) {sorted(unknown)!r}. "
            f"Known keys: {sorted(_KNOWN_TOP_LEVEL_KEYS)!r}"
        )

    # tommy.version — required
    tommy_section = raw.get("tommy")
    if not isinstance(tommy_section, dict) or "version" not in tommy_section:
        raise TommyYamlError(f"{source}: missing required `tommy.version` field")
    version = tommy_section["version"]
    _require_type(version, int, f"{source}: tommy.version")
    if version not in SUPPORTED_VERSIONS:
        raise TommyYamlError(
            f"{source}: tommy.version {version} is not supported "
            f"(supported: {SUPPORTED_VERSIONS!r})"
        )

    cfg = TommyYamlConfig(version=version)

    # project
    proj_raw = raw.get("project") or {}
    _require_type(proj_raw, dict, f"{source}: project")
    for k in ("name", "key", "git_root"):
        if k in proj_raw:
            _require_type(proj_raw[k], str, f"{source}: project.{k}")
    cfg.project = ProjectBlock(
        name=proj_raw.get("name", ""),
        key=proj_raw.get("key", ""),
        git_root=proj_raw.get("git_root"),
    )

    # memnos
    memnos_raw = raw.get("memnos") or {}
    _require_type(memnos_raw, dict, f"{source}: memnos")
    if "namespace" in memnos_raw:
        _require_type(memnos_raw["namespace"], str, f"{source}: memnos.namespace")
    cfg.memnos = MemnosBlock(namespace=memnos_raw.get("namespace", ""))

    # design_docs
    design_docs_raw = raw.get("design_docs") or []
    _require_type(design_docs_raw, list, f"{source}: design_docs")
    for i, pattern in enumerate(design_docs_raw):
        _require_type(pattern, str, f"{source}: design_docs[{i}]")
    cfg.design_docs = list(design_docs_raw)

    # corpus
    corpus_raw = raw.get("corpus") or {}
    _require_type(corpus_raw, dict, f"{source}: corpus")
    if "corpus_gate" in corpus_raw:
        _require_type(corpus_raw["corpus_gate"], bool, f"{source}: corpus.corpus_gate")
    if "auto_ingest" in corpus_raw:
        _require_type(corpus_raw["auto_ingest"], bool, f"{source}: corpus.auto_ingest")
    cfg.corpus = CorpusBlock(
        corpus_gate=corpus_raw.get("corpus_gate"),
        auto_ingest=corpus_raw.get("auto_ingest"),
    )

    # agents
    agents_raw = raw.get("agents") or {}
    _require_type(agents_raw, dict, f"{source}: agents")
    if "default_model" in agents_raw:
        _require_type(agents_raw["default_model"], str, f"{source}: agents.default_model")
    if "harness" in agents_raw:
        _require_type(agents_raw["harness"], str, f"{source}: agents.harness")
    for k in ("smart_routing", "mcp_introspect", "skip_permissions"):
        if k in agents_raw:
            _require_type(agents_raw[k], bool, f"{source}: agents.{k}")
    cfg.agents = AgentsBlock(
        default_model=agents_raw.get("default_model"),
        harness=agents_raw.get("harness"),
        smart_routing=agents_raw.get("smart_routing"),
        mcp_introspect=agents_raw.get("mcp_introspect"),
        skip_permissions=agents_raw.get("skip_permissions"),
    )

    # merge_gate / wave_limit
    if "merge_gate" in raw:
        _require_type(raw["merge_gate"], bool, f"{source}: merge_gate")
        cfg.merge_gate = raw["merge_gate"]
    if "wave_limit" in raw:
        _require_type(raw["wave_limit"], int, f"{source}: wave_limit")
        if raw["wave_limit"] < 0:
            raise TommyYamlError(f"{source}: wave_limit must be >= 0, got {raw['wave_limit']}")
        cfg.wave_limit = raw["wave_limit"]

    return cfg


def load_tommy_yaml(path: Path) -> TommyYamlConfig:
    """Load and validate a tommy.yaml file from disk."""
    path = Path(path)
    text = path.read_text()
    cfg = parse_tommy_yaml(text, source=str(path))
    cfg.source_path = path
    return cfg


def find_tommy_yaml(start: Optional[Path] = None) -> Optional[Path]:
    """Discover tommy.yaml by walking up from `start` (default: CWD).

    Stops as soon as a tommy.yaml is found. If none is found by the time a
    `.git` directory is reached, that directory is checked once more (repo
    root is the conventional home for tommy.yaml) and the search then stops —
    it never walks past the repo root or all the way to the filesystem root.
    """
    here = Path(start or Path.cwd()).resolve()
    for candidate in [here, *here.parents]:
        yaml_path = candidate / "tommy.yaml"
        if yaml_path.is_file():
            return yaml_path
        if (candidate / ".git").exists():
            # Reached the repo root without finding tommy.yaml along the way —
            # repo root is the conventional home for it, and we just checked
            # it above, so stop here rather than walking past the repo.
            return None
    return None
