"""
engram_orchestrator.planner — LLM-backed task decomposition planner.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import anthropic

logger = logging.getLogger(__name__)

PLANNER_SYSTEM = """You are a task decomposition planner for an AI orchestration system.
Break complex tasks into 1-5 independent parallel subtasks.
Each subtask must be completable independently (no subtask depends on another's result).
If the task is simple, return a single subtask with the original prompt.

You have context from:
1. Similar past tasks that succeeded (use as guidance)
2. Heuristic rules derived from past failures (follow these)
3. Skill templates for known task patterns (reuse these approaches)

Return ONLY valid JSON array, no other text:
[{"id": "1", "prompt": "...", "agent": null}]
"""

_FALLBACK_SINGLE = lambda task: [{"id": "1", "prompt": task, "agent": None}]  # noqa: E731


def _extract_json_array(text: str) -> list[dict[str, Any]] | None:
    """
    Try to extract a JSON array from `text`.
    Handles both clean JSON and JSON embedded in markdown code fences.
    """
    # Strip markdown code fences if present
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fenced:
        text = fenced.group(1).strip()

    # Try direct parse first
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed
    except json.JSONDecodeError:
        pass

    # Try to find a JSON array anywhere in the text
    bracket_match = re.search(r"\[[\s\S]*\]", text)
    if bracket_match:
        try:
            parsed = json.loads(bracket_match.group(0))
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass

    return None


class Planner:
    """
    Decomposes a user task into parallel subtasks using an LLM.

    Returns a list of dicts with keys: id, prompt, agent.
    Falls back to a single-subtask list if parsing fails.
    """

    def __init__(self, api_key: str, model: str) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self._model = model

    async def decompose(
        self,
        task: str,
        past_context: str = "",
        heuristics: str = "",
        template: str = "",
    ) -> list[dict[str, Any]]:
        """
        Break `task` into 1-5 parallel subtasks.

        Parameters
        ----------
        task:
            The user's original task prompt.
        past_context:
            Bullet-list summary of similar past successful tasks.
        heuristics:
            Numbered rules derived from past failures.
        template:
            A skill template for this type of task, if one was matched.

        Returns
        -------
        List of dicts: [{"id": str, "prompt": str, "agent": str | None}]
        """
        # Build the user message with available context
        context_sections: list[str] = []

        if past_context:
            context_sections.append(f"## Past successful tasks\n{past_context}")

        if heuristics:
            context_sections.append(f"## Heuristic rules (follow these)\n{heuristics}")

        if template:
            context_sections.append(f"## Skill template (reuse this approach)\n{template}")

        if context_sections:
            context_block = "\n\n".join(context_sections) + "\n\n"
        else:
            context_block = ""

        user_message = f"{context_block}Task to decompose:\n{task}"

        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=2048,
                system=PLANNER_SYSTEM,
                messages=[{"role": "user", "content": user_message}],
            )
        except Exception as exc:
            logger.warning("Planner API call failed: %s — returning single subtask", exc)
            return _FALLBACK_SINGLE(task)

        # Extract text from response
        raw_text = ""
        for block in response.content:
            if block.type == "text":
                raw_text += block.text

        raw_text = raw_text.strip()
        logger.debug("Planner raw response: %r", raw_text[:500])

        parsed = _extract_json_array(raw_text)

        if parsed is None:
            logger.warning("Planner failed to parse JSON response — returning single subtask")
            return _FALLBACK_SINGLE(task)

        # Normalise each entry
        normalised: list[dict[str, Any]] = []
        for i, entry in enumerate(parsed):
            if not isinstance(entry, dict):
                continue
            normalised.append(
                {
                    "id": str(entry.get("id", i + 1)),
                    "prompt": str(entry.get("prompt", task)),
                    "agent": entry.get("agent"),  # may be None
                }
            )

        if not normalised:
            logger.warning("Planner returned empty list — returning single subtask")
            return _FALLBACK_SINGLE(task)

        return normalised
