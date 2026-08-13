"""Pluggable LLM backends.

Agents depend on the :class:`~research_desk.llm.base.LLMProvider` protocol only;
which vendor answers is decided by configuration at startup.
"""

from research_desk.llm.base import (
    ChatRequest,
    LLMConfigurationError,
    LLMError,
    LLMProvider,
)
from research_desk.llm.gemini import GeminiProvider
from research_desk.llm.groq import GroqProvider
from research_desk.llm.registry import (
    SUPPORTED_PROVIDERS,
    build_provider,
    build_provider_for,
    parse_model_spec,
)
from research_desk.llm.stub import StubProvider

__all__ = [
    "SUPPORTED_PROVIDERS",
    "ChatRequest",
    "GeminiProvider",
    "GroqProvider",
    "LLMConfigurationError",
    "LLMError",
    "LLMProvider",
    "StubProvider",
    "build_provider",
    "build_provider_for",
    "parse_model_spec",
]
