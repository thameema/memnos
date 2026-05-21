"""
engram_orchestrator.critic — Evaluates a draft response and optionally returns corrections.
"""

from __future__ import annotations

import logging

import anthropic

logger = logging.getLogger(__name__)

_DEFAULT_CRITIC_SYSTEM = """You are a strict quality reviewer for AI-generated responses.

Review the draft response against the original task. Check for:
- Completeness: does it fully address the task?
- Accuracy: are the claims correct and well-supported?
- Clarity: is the response clear and well-structured?
- Relevance: is all content relevant to the task?

If the response is satisfactory, reply with exactly: LGTM

If corrections are needed, reply with a concise description of what must be fixed
and/or an improved version. Do NOT say LGTM if any issues exist.
"""


class CriticWorker:
    """
    Evaluates a draft response against the original task.

    Returns (True, None) if the draft passes, or (False, corrections_text) if not.
    """

    def __init__(self, api_key: str, model: str) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self._model = model

    async def evaluate(
        self,
        task: str,
        draft: str,
        agent_system_prompt: str = "",
        critic_prompt: str = "",
    ) -> tuple[bool, str | None]:
        """
        Evaluate a draft response.

        Parameters
        ----------
        task:
            The original user task prompt.
        draft:
            The draft response to evaluate.
        agent_system_prompt:
            Optional system prompt from the agent, for context on intent.
        critic_prompt:
            Optional additional instructions for this specific critic evaluation.

        Returns
        -------
        (True, None)            — draft passes, no corrections needed.
        (False, corrections)    — draft fails, corrections is a non-empty string.
        """
        system = _DEFAULT_CRITIC_SYSTEM
        if critic_prompt:
            system = f"{system}\n\nAdditional review criteria:\n{critic_prompt}"

        sections: list[str] = [f"## Task\n{task}"]
        if agent_system_prompt:
            sections.append(f"## Agent Instructions\n{agent_system_prompt}")
        sections.append(f"## Draft Response\n{draft}")

        user_message = "\n\n".join(sections)

        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=2048,
                system=system,
                messages=[{"role": "user", "content": user_message}],
            )
        except Exception as exc:
            logger.error("CriticWorker API call failed: %s — passing draft", exc)
            return True, None

        critic_text = ""
        for block in response.content:
            if block.type == "text":
                critic_text += block.text

        critic_text = critic_text.strip()
        logger.debug("CriticWorker response: %r", critic_text[:300])

        if critic_text.upper().startswith("LGTM"):
            return True, None

        return False, critic_text if critic_text else None
