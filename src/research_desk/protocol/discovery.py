"""Agent discovery.

The coordinator is configured with peer *base URLs* only — it is told where to
look, never what it will find. At startup it fetches each peer's agent card from
``/.well-known/agent-card.json`` and indexes the skills advertised there. All
later routing is a lookup in that index by skill id.

This is the "well-known URI" discovery mechanism from the A2A specification,
seeded by direct configuration. A production system would more likely query a
registry service; the lookup interface below would not change.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx
from a2a.client import A2ACardResolver
from a2a.types import a2a_pb2

from research_desk.logging import get_logger

logger = get_logger(__name__)


class SkillNotAvailableError(LookupError):
    """No discovered agent advertises the requested skill."""


@dataclass(frozen=True, slots=True)
class DiscoveredAgent:
    """A peer and the card it published."""

    base_url: str
    card: a2a_pb2.AgentCard

    @property
    def name(self) -> str:
        return self.card.name

    @property
    def skill_ids(self) -> list[str]:
        return [skill.id for skill in self.card.skills]

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.card.name,
            "description": self.card.description,
            "version": self.card.version,
            "base_url": self.base_url,
            "interfaces": [
                {
                    "url": interface.url,
                    "protocol_binding": interface.protocol_binding,
                    "protocol_version": interface.protocol_version,
                }
                for interface in self.card.supported_interfaces
            ],
            "capabilities": {
                "streaming": self.card.capabilities.streaming,
                "push_notifications": self.card.capabilities.push_notifications,
            },
            "skills": [
                {"id": skill.id, "name": skill.name, "tags": list(skill.tags)}
                for skill in self.card.skills
            ],
        }


class AgentRegistry:
    """Fetches peer agent cards and indexes them by skill id."""

    def __init__(
        self,
        peer_urls: list[str],
        http_client: httpx.AsyncClient,
        *,
        retries: int = 10,
        retry_delay_seconds: float = 1.5,
    ) -> None:
        self._peer_urls = [url.rstrip("/") for url in peer_urls]
        self._http_client = http_client
        self._retries = max(1, retries)
        self._retry_delay = retry_delay_seconds
        self._agents: list[DiscoveredAgent] = []
        self._by_skill: dict[str, DiscoveredAgent] = {}

    @property
    def agents(self) -> list[DiscoveredAgent]:
        return list(self._agents)

    @property
    def skill_ids(self) -> list[str]:
        return sorted(self._by_skill)

    async def discover(self) -> list[DiscoveredAgent]:
        """Resolve every configured peer, retrying while peers are still booting.

        Peers that never answer are logged and skipped rather than fatal: the
        coordinator degrades to the specialists that did come up.
        """
        results = await asyncio.gather(
            *(self._resolve(url) for url in self._peer_urls),
            return_exceptions=False,
        )
        self._agents = [agent for agent in results if agent is not None]
        self._by_skill = {}
        for agent in self._agents:
            for skill_id in agent.skill_ids:
                if (existing := self._by_skill.get(skill_id)) is not None:
                    logger.warning(
                        "duplicate skill advertised, keeping first",
                        extra={"skill": skill_id, "kept": existing.name, "ignored": agent.name},
                    )
                    continue
                self._by_skill[skill_id] = agent

        logger.info(
            "discovery complete",
            extra={
                "agents": len(self._agents),
                "expected": len(self._peer_urls),
                "skills": ",".join(self.skill_ids) or "none",
            },
        )
        return self.agents

    def by_skill(self, skill_id: str) -> DiscoveredAgent:
        """Find the agent advertising ``skill_id``."""
        try:
            return self._by_skill[skill_id]
        except KeyError:
            raise SkillNotAvailableError(
                f"No discovered agent offers the skill '{skill_id}'. "
                f"Available: {', '.join(self.skill_ids) or 'none'}."
            ) from None

    def has_skill(self, skill_id: str) -> bool:
        return skill_id in self._by_skill

    async def _resolve(self, base_url: str) -> DiscoveredAgent | None:
        resolver = A2ACardResolver(httpx_client=self._http_client, base_url=base_url)
        for attempt in range(1, self._retries + 1):
            try:
                card = await resolver.get_agent_card()
            except Exception as exc:  # noqa: BLE001 - peer may simply not be up yet
                logger.debug(
                    "peer not ready",
                    extra={"peer_url": base_url, "attempt": attempt, "error": str(exc)},
                )
                if attempt == self._retries:
                    logger.error(
                        "peer discovery failed",
                        extra={"peer_url": base_url, "attempts": attempt, "error": str(exc)},
                    )
                    return None
                await asyncio.sleep(self._retry_delay)
                continue

            logger.info(
                "discovered agent",
                extra={
                    "peer": card.name,
                    "peer_url": base_url,
                    "skills": ",".join(skill.id for skill in card.skills),
                },
            )
            return DiscoveredAgent(base_url=base_url, card=card)
        return None
