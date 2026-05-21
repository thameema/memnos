"""
engram_orchestrator.synthesizer — Combines parallel subtask results into a single response.
"""

from __future__ import annotations

import logging

import anthropic

logger = logging.getLogger(__name__)

SYNTHESIZER_SYSTEM = """You are synthesistartup-corp results from parallel worker agents.
Combine the results into a single coherent response to the original task.
Be concise but complete. Do not repeat information unnecessarily.
"""


class Synthesizer:
    """
    Calls the LLM to merge multiple subtask results into one coherent response.
    """

    def __init__(self, api_key: str, model: str) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self._model = model

    async def synthesize(
        self,
        original_task: str,
        subtask_results: list[tuple[str, str]],
    ) -> str:
        """
        Merge subtask results into a single response.

        Parameters
        ----------
        original_task:
            The original user prompt that was decomposed.
        subtask_results:
            List of (subtask_prompt, result_text) tuples.

        Returns
        -------
        A single string containing the synthesized response.
        """
        if not subtask_results:
            return "No subtask results to synthesize."

        # Build the synthesis prompt
        sections: list[str] = [f"## Original Task\n{original_task}\n"]

        for i, (sub_prompt, sub_result) in enumerate(subtask_results, start=1):
            sections.append(
                f"## Worker {i} Result\n"
                f"**Subtask:** {sub_prompt}\n\n"
                f"**Result:**\n{sub_result}"
            )

        user_message = "\n\n".join(sections)

        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=8096,
                system=SYNTHESIZER_SYSTEM,
                messages=[{"role": "user", "content": user_message}],
            )
        except Exception as exc:
            logger.error("Synthesizer API call failed: %s", exc)
            # Fall back to concatenating results
            fallback_parts = [f"[Subtask: {p}]\n{r}" for p, r in subtask_results]
            return "\n\n---\n\n".join(fallback_parts)

        for block in response.content:
            if block.type == "text":
                return block.text

        return ""
