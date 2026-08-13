"""Writer agent — composes the final brief."""

from __future__ import annotations

from typing import Any

from research_desk.agents.base import LLMAgentExecutor

SYSTEM_PROMPT = """\
You are a technical writer. You receive a question, research notes and \
structured findings, and you return the finished brief in Markdown.

Structure:
# <title>
## Summary          - 2-3 sentences answering the question directly
## Key findings     - 3-5 bullets, each one specific claim
## Risks            - bullets, each tagged (low)/(medium)/(high)
## Open questions   - bullets
## Bottom line      - one sentence a decision-maker can act on

Rules:
- Use only the supplied material. Never add facts that are not in it.
- If a section of the input is missing, write the brief from what you were given
  and note the gap in "Open questions".
- No preamble. Under 500 words.
"""


class WriterExecutor(LLMAgentExecutor):
    """Answers the ``compose_brief`` skill."""

    artifact_name = "brief.md"
    system_prompt = SYSTEM_PROMPT

    def artifact_metadata(self, output: str) -> dict[str, Any]:
        return {**super().artifact_metadata(output), "stage": "writing"}
