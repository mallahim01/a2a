"""Shared HTTP plumbing for the hosted providers.

Both vendor clients are thin: build a request body, post it, read one field out
of the response. What is *not* trivial — and is worth sharing — is the failover
policy, so it lives here once.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import httpx

from research_desk.llm.base import LLMConfigurationError, LLMError
from research_desk.logging import get_logger

logger = get_logger(__name__)

#: Statuses that mean "this key is spent" — move to the next key in the pool.
ROTATE_STATUSES = frozenset({401, 403, 429})
#: Statuses worth retrying on the same key after a short back-off.
RETRY_STATUSES = frozenset({500, 502, 503, 504})


class RotatingKeyClient:
    """Posts JSON to one endpoint, failing over across a pool of API keys.

    ``.env`` holds several keys per vendor precisely because free tiers are
    rate-limited; treating them as an ordered pool turns a 429 into a retry
    instead of a failed task.
    """

    def __init__(
        self,
        *,
        provider: str,
        api_keys: list[str],
        timeout: float,
        http_client: httpx.AsyncClient | None = None,
        attempts_per_key: int = 2,
        backoff_seconds: float = 1.0,
    ) -> None:
        if not api_keys:
            raise LLMConfigurationError(
                f"No API keys configured for provider '{provider}'. "
                f"Set {provider.upper()}_API_KEY in the environment, "
                f"or point the agent at 'stub:<name>' to run without credentials."
            )
        self._provider = provider
        self._api_keys = api_keys
        self._attempts_per_key = attempts_per_key
        self._backoff_seconds = backoff_seconds
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(timeout=timeout)

    async def post_json(
        self,
        url: str,
        *,
        body: Mapping[str, Any],
        headers_for_key: Any,
    ) -> dict[str, Any]:
        """POST ``body``, rotating keys on quota errors.

        ``headers_for_key`` maps an API key to the request headers that carry it,
        which is the only part that differs between vendors.
        """
        last_error: str = "no attempt was made"

        for key_index, api_key in enumerate(self._api_keys, start=1):
            for attempt in range(1, self._attempts_per_key + 1):
                try:
                    response = await self._client.post(
                        url, json=dict(body), headers=headers_for_key(api_key)
                    )
                except httpx.HTTPError as exc:
                    last_error = f"transport error: {exc!r}"
                    logger.warning(
                        "llm request failed",
                        extra={
                            "provider": self._provider,
                            "key_index": key_index,
                            "attempt": attempt,
                            "error": str(exc),
                        },
                    )
                    await self._pause(attempt)
                    continue

                if response.status_code == httpx.codes.OK:
                    return dict(response.json())

                last_error = f"HTTP {response.status_code}: {_short(response.text)}"

                if response.status_code in ROTATE_STATUSES:
                    logger.warning(
                        "llm key rejected, rotating",
                        extra={
                            "provider": self._provider,
                            "key_index": key_index,
                            "status": response.status_code,
                        },
                    )
                    break

                if response.status_code in RETRY_STATUSES:
                    await self._pause(attempt)
                    continue

                raise LLMError(f"{self._provider} rejected the request — {last_error}")

        raise LLMError(
            f"{self._provider} could not complete the request after trying "
            f"{len(self._api_keys)} key(s) — {last_error}"
        )

    async def _pause(self, attempt: int) -> None:
        await asyncio.sleep(self._backoff_seconds * attempt)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _short(text: str, limit: int = 200) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else f"{collapsed[:limit]}…"
