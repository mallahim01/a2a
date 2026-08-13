"""Google Gemini provider (Generative Language API)."""

from __future__ import annotations

from typing import Any

import httpx

from research_desk.llm._http import RotatingKeyClient
from research_desk.llm.base import ChatRequest, LLMError
from research_desk.logging import get_logger

API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"

logger = get_logger(__name__)


class GeminiProvider:
    """Talks to the Gemini ``generateContent`` endpoint over plain HTTP.

    Used by the researcher agent. Google Search grounding is available but off by
    default: it is not part of the free tier and returns 429 without a paid plan.
    """

    def __init__(
        self,
        model: str,
        api_keys: list[str],
        *,
        timeout: float = 90.0,
        enable_search: bool = False,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._model = model
        self._enable_search = enable_search
        self._client = RotatingKeyClient(
            provider="google",
            api_keys=api_keys,
            timeout=timeout,
            http_client=http_client,
        )

    @property
    def name(self) -> str:
        suffix = "+search" if self._enable_search else ""
        return f"gemini:{self._model}{suffix}"

    async def complete(self, request: ChatRequest) -> str:
        body: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": request.system}]},
            "contents": [{"role": "user", "parts": [{"text": request.user}]}],
            "generationConfig": {
                "temperature": request.temperature,
                "maxOutputTokens": request.max_output_tokens,
            },
        }
        if request.json_object:
            body["generationConfig"]["responseMimeType"] = "application/json"
        if self._enable_search:
            body["tools"] = [{"google_search": {}}]

        payload = await self._client.post_json(
            f"{API_ROOT}/{self._model}:generateContent",
            body=body,
            headers_for_key=lambda key: {"x-goog-api-key": key},
        )

        text = _first_candidate_text(payload)
        if not text:
            raise LLMError(f"Gemini returned no usable text: {_diagnose(payload)}")
        return text

    async def aclose(self) -> None:
        await self._client.aclose()


def _first_candidate_text(payload: dict[str, Any]) -> str:
    candidates = payload.get("candidates") or []
    if not candidates:
        return ""
    parts = candidates[0].get("content", {}).get("parts") or []
    chunks = [part["text"] for part in parts if isinstance(part.get("text"), str)]
    return "\n".join(chunks).strip()


def _diagnose(payload: dict[str, Any]) -> str:
    """Explain an empty response — usually a safety block or a token cut-off."""
    candidates = payload.get("candidates") or []
    if candidates and (reason := candidates[0].get("finishReason")):
        return f"finishReason={reason}"
    if feedback := payload.get("promptFeedback"):
        return f"promptFeedback={feedback}"
    return "empty candidate list"
