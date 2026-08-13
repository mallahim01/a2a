"""Shared fixtures.

The important one is :func:`desk`, which runs all four agents inside the test
process and routes their A2A traffic over an in-process ASGI transport. The
agents still exchange real JSON-RPC through the real protocol stack — only the
socket is replaced — so the end-to-end tests cover the same code paths that run
in Docker, without network access or API keys.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
import pytest
from starlette.applications import Starlette

from research_desk.agents import build_agent_app
from research_desk.config import DEFAULT_PORTS, AgentName, Settings

SPECIALISTS = (AgentName.RESEARCHER, AgentName.ANALYST, AgentName.WRITER)

STUB_MODELS = {
    "coordinator_model": "stub:planner",
    "researcher_model": "stub:researcher",
    "analyst_model": "stub:analyst",
    "writer_model": "stub:writer",
}


def agent_url(agent: AgentName) -> str:
    """The URL an agent advertises in its card inside the test network."""
    return f"http://{agent.value}.test:{DEFAULT_PORTS[agent]}"


def authority(agent: AgentName) -> str:
    return urlsplit(agent_url(agent)).netloc


class RoutingTransport(httpx.AsyncBaseTransport):
    """Dispatches requests to one of several ASGI apps, keyed by URL authority."""

    def __init__(self) -> None:
        self._transports: dict[str, httpx.ASGITransport] = {}

    def mount(self, netloc: str, app: Starlette) -> None:
        self._transports[netloc] = httpx.ASGITransport(app=app)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        target = self._transports.get(request.url.netloc.decode())
        if target is None:
            raise httpx.ConnectError(
                f"no agent listening on {request.url.netloc.decode()}", request=request
            )
        return await target.handle_async_request(request)


@dataclass(frozen=True)
class Desk:
    """A running four-agent desk and an HTTP client wired into it."""

    client: httpx.AsyncClient
    settings: Settings

    def url(self, agent: AgentName) -> str:
        return agent_url(agent)


def build_settings(**overrides: object) -> Settings:
    """Settings for tests: stub models, no .env, in-test peer URLs."""
    values: dict[str, object] = {
        **STUB_MODELS,
        "peer_agent_urls": [agent_url(agent) for agent in SPECIALISTS],
        "discovery_retries": 1,
        "discovery_retry_delay_seconds": 0.0,
        **overrides,
    }
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def _scoped(settings: Settings, agent: AgentName) -> Settings:
    """Each agent needs the absolute URL its own card should advertise."""
    return settings.model_copy(update={"public_url": f"{agent_url(agent)}/"})


@asynccontextmanager
async def open_desk(
    *,
    offline: set[AgentName] | None = None,
    **overrides: object,
) -> AsyncIterator[Desk]:
    """Start a desk, optionally with some specialists missing."""
    offline = offline or set()
    settings = build_settings(**overrides)
    transport = RoutingTransport()

    for agent in SPECIALISTS:
        if agent not in offline:
            transport.mount(authority(agent), build_agent_app(agent, _scoped(settings, agent)))

    # The coordinator's outbound calls travel over the same in-process transport,
    # which is exactly what the injectable http client exists for.
    async with httpx.AsyncClient(transport=transport, timeout=30) as peer_client:
        transport.mount(
            authority(AgentName.COORDINATOR),
            build_agent_app(
                AgentName.COORDINATOR,
                _scoped(settings, AgentName.COORDINATOR),
                http_client=peer_client,
            ),
        )
        async with httpx.AsyncClient(transport=transport, timeout=30) as client:
            yield Desk(client=client, settings=settings)


@pytest.fixture
def settings() -> Settings:
    return build_settings()


@pytest.fixture
async def desk() -> AsyncIterator[Desk]:
    async with open_desk() as running:
        yield running


@pytest.fixture
def make_desk() -> Callable[..., object]:
    """Async context manager factory for degradation scenarios."""
    return open_desk
