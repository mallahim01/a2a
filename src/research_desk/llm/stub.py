"""Deterministic offline provider.

Selected with ``<agent>_MODEL=stub:<label>``. It lets the whole four-agent
collaboration — discovery, delegation, task lifecycle, artifacts — be exercised
by the test suite and by anyone cloning the repo without API keys. The protocol
behaviour is identical; only the prose is synthetic.
"""

from __future__ import annotations

import json

from research_desk.llm.base import ChatRequest


class StubProvider:
    """Echoes a structured, prompt-derived answer instead of calling a model."""

    def __init__(self, label: str = "offline") -> None:
        self._label = label

    @property
    def name(self) -> str:
        return f"stub:{self._label}"

    async def complete(self, request: ChatRequest) -> str:
        topic = _topic(request.user)
        if request.json_object:
            return json.dumps(
                {
                    "themes": [f"{topic} — adoption", f"{topic} — tooling"],
                    "risks": [f"{topic} standards may fragment"],
                    "open_questions": [f"How mature is {topic} in production?"],
                    "confidence": "low",
                    "source": self.name,
                },
                indent=2,
            )
        return (
            f"## {topic}\n\n"
            f"Synthetic output from `{self.name}` (no model was called).\n\n"
            f"- Point one about {topic}.\n"
            f"- Point two about {topic}.\n"
        )

    async def aclose(self) -> None:
        return None


def _topic(prompt: str) -> str:
    """Best-effort subject line so stub output visibly tracks the real query."""
    for line in prompt.splitlines():
        cleaned = line.strip().lstrip("#-* ").strip()
        if len(cleaned) > 3 and not cleaned.endswith(":"):
            return cleaned[:120]
    return "the requested topic"
