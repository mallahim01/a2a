"""Resolves a ``provider:model`` string into a concrete provider instance."""

from __future__ import annotations

import httpx

from research_desk.config import AgentName, Settings
from research_desk.llm.base import LLMConfigurationError, LLMProvider
from research_desk.llm.gemini import GeminiProvider
from research_desk.llm.groq import GroqProvider
from research_desk.llm.stub import StubProvider

SUPPORTED_PROVIDERS = ("groq", "gemini", "stub")


def parse_model_spec(spec: str) -> tuple[str, str]:
    """Split ``"groq:llama-3.3-70b-versatile"`` into ``("groq", "llama-…")``.

    Model names may themselves contain a slash (``openai/gpt-oss-120b``) but never
    a colon, so a single split is unambiguous.
    """
    provider, _, model = spec.partition(":")
    provider = provider.strip().lower()
    model = model.strip()

    if not provider or not model:
        raise LLMConfigurationError(
            f"Invalid model spec {spec!r}. Expected '<provider>:<model>', "
            f"where provider is one of {', '.join(SUPPORTED_PROVIDERS)}."
        )
    if provider not in SUPPORTED_PROVIDERS:
        raise LLMConfigurationError(
            f"Unknown provider {provider!r} in {spec!r}. "
            f"Supported: {', '.join(SUPPORTED_PROVIDERS)}."
        )
    return provider, model


def build_provider(
    spec: str,
    settings: Settings,
    *,
    enable_search: bool = False,
    http_client: httpx.AsyncClient | None = None,
) -> LLMProvider:
    """Instantiate the provider named by ``spec`` using credentials from settings."""
    provider, model = parse_model_spec(spec)

    if provider == "stub":
        return StubProvider(model)
    if provider == "groq":
        return GroqProvider(
            model,
            settings.groq_api_keys,
            timeout=settings.llm_timeout_seconds,
            http_client=http_client,
        )
    return GeminiProvider(
        model,
        settings.google_api_keys,
        timeout=settings.llm_timeout_seconds,
        enable_search=enable_search,
        http_client=http_client,
    )


def build_provider_for(
    agent: AgentName,
    settings: Settings,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> LLMProvider:
    """Build the provider configured for one agent."""
    return build_provider(
        settings.model_for(agent),
        settings,
        enable_search=agent is AgentName.RESEARCHER and settings.researcher_enable_search,
        http_client=http_client,
    )
