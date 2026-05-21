import importlib
import importlib.util
import inspect
import logging
from pathlib import Path

from engram.skills.decorator import SkillDefinition

logger = logging.getLogger(__name__)


def load_skills(skills_dir: Path) -> list[SkillDefinition]:
    """Auto-discover and load all @skill-decorated functions from a directory."""
    skills: list[SkillDefinition] = []
    skills_dir = Path(skills_dir)

    if not skills_dir.exists():
        return skills

    for path in sorted(skills_dir.rglob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(
                f"engram_skills.{path.stem}", path
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            for _, fn in inspect.getmembers(module, inspect.isfunction):
                if hasattr(fn, "_engram_skill"):
                    skills.append(fn._engram_skill)
                    logger.debug("Loaded skill: %s from %s", fn._engram_skill.name, path)
        except Exception as exc:
            logger.warning("Failed to load skills from %s: %s", path, exc)

    logger.info("Loaded %d skills from %s", len(skills), skills_dir)
    return skills


def load_all_skills(repo_root: Path = None) -> list[SkillDefinition]:
    """Load builtin skills and user skills from the repo."""
    if repo_root is None:
        repo_root = Path(__file__).parent.parent

    all_skills: list[SkillDefinition] = []

    builtin_dir = Path(__file__).parent / "builtin"
    all_skills.extend(load_skills(builtin_dir))

    user_skills_dir = repo_root / "skills"
    if user_skills_dir != Path(__file__).parent:
        for path in user_skills_dir.glob("*.py"):
            if path.name not in ("__init__.py", "decorator.py", "loader.py"):
                all_skills.extend(load_skills(path.parent))
                break

    # Deduplicate by name (user skills override builtins)
    seen: dict[str, SkillDefinition] = {}
    for s in all_skills:
        seen[s.name] = s
    return list(seen.values())
