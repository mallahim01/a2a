"""The provider-agnostic surface every LLM backend implements.

Agents depend only on :class:`LLMProvider`, so swapping a model — or a vendor —
is an environment variable change rather than a code change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class LLMError(RuntimeError):
    """A completion could not be produced.

    Agents translate this into an A2A ``TASK_STATE_FAILED`` rather than letting
    it escape as an unhandled exception.
    """


class LLMConfigurationError(LLMError):
    """The requested provider or model string cannot be resolved."""


@dataclass(frozen=True, slots=True)
class ChatRequest:
    """A single-turn completion request.

    Deliberately minimal: the demo's agents are stateless single-shot workers, so
    a system prompt plus a user prompt covers every call made in this project.
    """

    system: str
    user: str
    max_output_tokens: int = 2048
    temperature: float = 0.3
    json_object: bool = False
    """Ask the provider to constrain output to a single JSON object."""


@runtime_checkable
class LLMProvider(Protocol):
    """What an agent needs from a model backend."""

    @property
    def name(self) -> str:
        """Identifier of the form ``provider:model``, surfaced in logs and cards."""
        ...

    async def complete(self, request: ChatRequest) -> str: ...

    async def aclose(self) -> None: ...
