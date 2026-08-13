"""Builds the ASGI application for whichever agent this process is running as.

One image, one entry point, four agents: which one you get is decided entirely
by the ``agent`` argument and the environment.
"""

from __future__ import annotations

import httpx
from a2a.server.agent_execution import AgentExecutor
from a2a.types import a2a_pb2
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from research_desk.agents.analyst import AnalystExecutor
from research_desk.agents.coordinator import CoordinatorExecutor
from research_desk.agents.researcher import ResearcherExecutor
from research_desk.agents.writer import WriterExecutor
from research_desk.cards import build_card
from research_desk.config import AgentName, Settings, get_settings
from research_desk.llm import LLMProvider, build_provider_for
from research_desk.logging import bind_agent, get_logger
from research_desk.protocol.client import PeerClient
from research_desk.protocol.discovery import AgentRegistry
from research_desk.protocol.server import build_app

logger = get_logger(__name__)

_SPECIALISTS: dict[AgentName, type[AgentExecutor]] = {
    AgentName.RESEARCHER: ResearcherExecutor,
    AgentName.ANALYST: AnalystExecutor,
    AgentName.WRITER: WriterExecutor,
}


def build_agent_app(
    agent: AgentName,
    settings: Settings | None = None,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> Starlette:
    """Assemble the server for one agent.

    ``http_client`` is injected by the test suite so that the coordinator's
    outbound A2A calls travel over an in-process ASGI transport instead of a
    real socket. Production leaves it unset and gets a normal HTTP client.
    """
    settings = settings or get_settings()
    bind_agent(agent.value)

    card = build_card(agent, settings.public_url_for(agent))
    provider = build_provider_for(agent, settings)

    if agent is AgentName.COORDINATOR:
        return _build_coordinator(settings, card=card, planner=provider, http_client=http_client)

    executor = _SPECIALISTS[agent](
        provider,
        agent_label=card.name,
        max_output_tokens=settings.llm_max_output_tokens,
        temperature=settings.llm_temperature,
    )
    logger.info(
        "agent configured",
        extra={
            "agent": agent.value,
            "model": provider.name,
            "url": card.supported_interfaces[0].url,
        },
    )
    return build_app(card=card, executor=executor, on_shutdown=provider.aclose)


def _build_coordinator(
    settings: Settings,
    *,
    card: a2a_pb2.AgentCard,
    planner: LLMProvider,
    http_client: httpx.AsyncClient | None,
) -> Starlette:
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=settings.peer_request_timeout_seconds)

    registry = AgentRegistry(
        settings.peer_agent_urls,
        client,
        retries=settings.discovery_retries,
        retry_delay_seconds=settings.discovery_retry_delay_seconds,
    )
    executor = CoordinatorExecutor(
        registry=registry,
        peer_client=PeerClient(client),
        planner=planner,
    )

    async def list_agents(_: Request) -> JSONResponse:
        """Inspection endpoint: what the coordinator discovered, and where."""
        return JSONResponse(
            {
                "configured_peers": settings.peer_agent_urls,
                "discovered": [agent.describe() for agent in registry.agents],
                "skills": registry.skill_ids,
            }
        )

    async def on_startup() -> None:
        logger.info(
            "coordinator starting",
            extra={"planner": planner.name, "peers": ",".join(settings.peer_agent_urls)},
        )
        await registry.discover()

    async def on_shutdown() -> None:
        await planner.aclose()
        if owns_client:
            await client.aclose()

    return build_app(
        card=card,
        executor=executor,
        extra_routes=[Route("/agents", list_agents, methods=["GET"])],
        on_startup=on_startup,
        on_shutdown=on_shutdown,
    )
