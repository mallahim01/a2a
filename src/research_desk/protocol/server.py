"""Assembles a runnable A2A server around an agent executor.

The SDK supplies the protocol routes; this module wires them into a Starlette
application and adds the two operational endpoints the demo needs (``/health``
and, for the coordinator, ``/agents``).

Routes exposed by every agent:

===========================  ======  ==================================
Path                         Method  Purpose
===========================  ======  ==================================
``/``                        POST    A2A JSON-RPC (SendMessage, GetTask…)
``/.well-known/agent-card``  GET     Agent card — discovery
``/health``                  GET     Liveness for Docker and humans
===========================  ======  ==================================
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from a2a.server.agent_execution import AgentExecutor
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import a2a_pb2
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from research_desk import __version__

Lifespan = Callable[[Starlette], Any]


def build_app(
    *,
    card: a2a_pb2.AgentCard,
    executor: AgentExecutor,
    extra_routes: list[Route] | None = None,
    on_startup: Callable[[], Any] | None = None,
    on_shutdown: Callable[[], Any] | None = None,
) -> Starlette:
    """Build the ASGI application for one agent.

    Task state is kept in :class:`InMemoryTaskStore`: fine for a demo, and the
    seam at which a real deployment would swap in the SDK's database store.
    """
    handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )

    async def health(_: Request) -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "agent": card.name,
                "version": __version__,
                "protocol_version": card.supported_interfaces[0].protocol_version,
                "skills": [skill.id for skill in card.skills],
            }
        )

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        if on_startup is not None:
            await on_startup()
        try:
            yield
        finally:
            if on_shutdown is not None:
                await on_shutdown()

    routes: list[Route] = [
        *create_agent_card_routes(card),
        Route("/health", health, methods=["GET"]),
        *(extra_routes or []),
        # The JSON-RPC route is mounted on "/" and matches greedily, so it goes last.
        *create_jsonrpc_routes(handler, "/"),
    ]
    return Starlette(routes=routes, lifespan=lifespan)
