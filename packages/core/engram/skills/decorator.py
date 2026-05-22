from dataclasses import dataclass
from typing import Callable, Any


@dataclass
class SkillDefinition:
    name: str
    description: str
    parameters: dict
    fn: Callable
    required: list[str] | None = None

    def to_anthropic_tool(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": {
                "type": "object",
                "properties": self.parameters,
                "required": self.required or list(self.parameters.keys()),
            },
        }

    def to_mcp_tool_schema(self) -> dict:
        return {
            "type": "object",
            "properties": self.parameters,
            "required": self.required or list(self.parameters.keys()),
        }


def skill(name: str, description: str, parameters: dict, required: list[str] | None = None):
    """Decorator to register a function as an engram skill (MCP tool)."""
    def decorator(fn: Callable) -> Callable:
        fn._engram_skill = SkillDefinition(
            name=name,
            description=description,
            parameters=parameters,
            fn=fn,
            required=required,
        )
        return fn
    return decorator
