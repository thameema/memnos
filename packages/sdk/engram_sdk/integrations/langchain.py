from __future__ import annotations

from typing import Any

try:
    from langchain_core.memory import BaseMemory
    from langchain_core.messages import BaseMessage  # noqa: F401
    _LANGCHAIN_AVAILABLE = True
except ImportError:
    _LANGCHAIN_AVAILABLE = False
    BaseMemory = object  # type: ignore[assignment,misc]

from engram_sdk.models import MemoryType


class EngramMemory(BaseMemory):
    """LangChain memory backend backed by engram.

    Usage:
        client = EngramClient(url="...", api_key="...")
        memory = EngramMemory(client=client, namespace="org:acme", session_id="session-123")
        agent = ConversationChain(llm=..., memory=memory)
    """

    client: Any
    namespace: str
    session_id: str
    memory_key: str = "history"
    input_key: str = "input"
    output_key: str = "output"

    def __init__(self, **data: Any) -> None:
        if not _LANGCHAIN_AVAILABLE:
            raise ImportError(
                "langchain-core is required for EngramMemory. "
                "Install it with: pip install 'engram-sdk[langchain]'"
            )
        super().__init__(**data)

    @property
    def memory_variables(self) -> list[str]:
        return [self.memory_key]

    def load_memory_variables(self, inputs: dict) -> dict:
        memories = self.client.search(
            f"session {self.session_id}",
            self.namespace,
            top_k=20,
        )
        history_lines: list[str] = []
        for m in sorted(memories, key=lambda x: x.created_at):
            if self.session_id in m.tags:
                history_lines.append(m.content)
        return {self.memory_key: "\n".join(history_lines)}

    def save_context(self, inputs: dict, outputs: dict) -> None:
        user_input = inputs.get(self.input_key, "")
        ai_output = outputs.get(self.output_key, "")
        combined = f"Human: {user_input}\nAI: {ai_output}"
        self.client.write(
            combined,
            self.namespace,
            memory_type=MemoryType.SESSION,
            tags=[self.session_id, "conversation"],
            rationale=f"LangChain session {self.session_id}",
            source="langchain",
        )

    def clear(self) -> None:
        # engram uses supersede for lifecycle management, not deletion
        pass
