"""
memnos.skill_coach — Proactive AI coding tool capability discovery.

Seeds a catalog of Claude Code features into the tool:claude-code:capabilities
namespace. The MCP skill_suggest tool uses this to surface relevant techniques
to developers based on what they are trying to do — without requiring developers
to know what to ask for.
"""
from memnos.skill_coach.seeder import seed_claude_code_capabilities
from memnos.skill_coach.suggester import suggest_skills

__all__ = ["seed_claude_code_capabilities", "suggest_skills"]
