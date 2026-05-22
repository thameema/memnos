"""
engram skills — @skill decorator and auto-discovery.

Usage:
    from engram.skills import skill

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

from engram.skills.decorator import skill, SkillDefinition  # noqa: F401

__all__ = ["skill", "SkillDefinition"]
