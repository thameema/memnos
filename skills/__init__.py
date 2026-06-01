"""
memnos skills — @skill decorator and auto-discovery.

Usage:
    from memnos.skills import skill

    @skill(
        name="my_tool",
        description="Does something useful",
        parameters={
            "input": {"type": "string", "description": "The input"}
        }
    )
    async def my_tool(input: str) -> dict:
        return {"result": input.upper()}
"""

from memnos.skills.decorator import skill, SkillDefinition

__all__ = ["skill", "SkillDefinition"]
