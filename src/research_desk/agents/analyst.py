"""Analyst agent — turns research notes into machine-readable findings."""

from __future__ import annotations

from typing import Any

from research_desk.agents.base import LLMAgentExecutor

SYSTEM_PROMPT = """\
You are an analyst. You receive raw research notes and return findings as a \
single JSON object — no prose, no Markdown fences.

Schema:
{
  "themes":         [{"title": str, "why_it_matters": str}],   // 3-5 entries
  "risks":          [{"risk": str, "severity": "low"|"medium"|"high"}],  // 2-4
  "open_questions": [str],                                     // 2-4
  "confidence":     "low"|"medium"|"high"
}

Rules:
- Ground every entry in the supplied notes; do not introduce outside claims.
- "confidence" reflects how well the notes support the findings, not how sure
  you are that the topic matters.
"""


class AnalystExecutor(LLMAgentExecutor):
    """Answers the ``extract_insights`` skill."""

    artifact_name = "findings.json"
    system_prompt = SYSTEM_PROMPT
    json_output = True

    def build_user_prompt(self, user_input: str) -> str:
        return f"Research notes to analyse:\n\n{user_input}"

    def artifact_metadata(self, output: str) -> dict[str, Any]:
        return {**super().artifact_metadata(output), "stage": "analysis"}
