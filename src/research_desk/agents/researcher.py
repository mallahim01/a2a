"""Researcher agent — gathers the factual landscape for a topic."""

from __future__ import annotations

from typing import Any

from research_desk.agents.base import LLMAgentExecutor

SYSTEM_PROMPT = """\
You are a research analyst preparing background material for a colleague who \
will turn it into a decision brief.

Produce a factual landscape of the topic in Markdown, under these headings:

## What it is
## Current state
## Key players and approaches
## Recent developments
## Points of disagreement
## Known unknowns

Rules:
- Be specific: name projects, organisations, standards and versions.
- Prefer verifiable facts over opinion, and say so plainly when something is contested.
- If you are not confident about a detail, mark it "(uncertain)" rather than inventing it.
- No preamble, no closing summary. Under 600 words.
"""


class ResearcherExecutor(LLMAgentExecutor):
    """Answers the ``gather_sources`` skill."""

    artifact_name = "research-notes.md"
    system_prompt = SYSTEM_PROMPT

    def build_user_prompt(self, user_input: str) -> str:
        return f"Topic to research:\n\n{user_input}"

    def artifact_metadata(self, output: str) -> dict[str, Any]:
        return {**super().artifact_metadata(output), "stage": "research"}
