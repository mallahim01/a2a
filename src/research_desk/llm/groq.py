"""Groq Cloud provider (OpenAI-compatible chat completions)."""

from __future__ import annotations

from typing import Any

import httpx

from research_desk.llm._http import RotatingKeyClient
from research_desk.llm.base import ChatRequest, LLMError

API_URL = "https://api.groq.com/openai/v1/chat/completions"


class GroqProvider:
    """Talks to Groq's OpenAI-compatible endpoint over plain HTTP.

    Used by the analyst and writer agents. Supports JSON-object mode, which the
    analyst relies on to return machine-readable insights.
    """

    def __init__(
        self,
        model: str,
        api_keys: list[str],
        *,
        timeout: float = 90.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._model = model
        self._client = RotatingKeyClient(
            provider="groq",
            api_keys=api_keys,
            timeout=timeout,
            http_client=http_client,
        )

    @property
    def name(self) -> str:
        return f"groq:{self._model}"

    async def complete(self, request: ChatRequest) -> str:
        body: dict[str, Any] = {
            "model": self._model,
            "temperature": request.temperature,
            "max_tokens": request.max_output_tokens,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
        }
        if request.json_object:
            body["response_format"] = {"type": "json_object"}

        payload = await self._client.post_json(
            API_URL,
            body=body,
            headers_for_key=lambda key: {"Authorization": f"Bearer {key}"},
        )

        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"Unexpected Groq response shape: {exc!r}") from exc

        if not content or not content.strip():
            raise LLMError("Groq returned an empty completion")
        return str(content).strip()

    async def aclose(self) -> None:
        await self._client.aclose()
