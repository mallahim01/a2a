"""Command line entry point.

    research-desk serve <agent>   run one agent as an A2A server
    research-desk dev             run all four locally, one process
    research-desk ask "<question>"  ask the coordinator, streaming the collaboration
    research-desk card <url>      fetch and print an agent card

``ask`` is a genuine A2A client: it resolves the coordinator's agent card, opens
a streaming ``SendMessage`` call, and renders the task lifecycle as it arrives.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import httpx
import uvicorn
from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
from a2a.helpers import display_agent_card, get_artifact_text, get_message_text
from a2a.types import a2a_pb2

from research_desk import __version__
from research_desk.agents import build_agent_app
from research_desk.config import AgentName, get_settings
from research_desk.logging import bind_agent, configure_logging, get_logger

logger = get_logger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)

    if args.command == "serve":
        return _serve(AgentName(args.agent))
    if args.command == "dev":
        return asyncio.run(_dev())
    if args.command == "ask":
        return asyncio.run(_ask(args.question, args.url))
    if args.command == "card":
        return asyncio.run(_card(args.url))

    parser.print_help()
    return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="research-desk",
        description="A2A protocol demo: four agents collaborating on a research brief.",
    )
    parser.add_argument("--version", action="version", version=f"research-desk {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run a single agent as an A2A server")
    serve.add_argument("agent", choices=[a.value for a in AgentName])

    sub.add_parser("dev", help="run all four agents locally in one process")

    ask = sub.add_parser("ask", help="send a question to the coordinator over A2A")
    ask.add_argument("question")
    ask.add_argument("--url", default="http://127.0.0.1:8000", help="coordinator base URL")

    card = sub.add_parser("card", help="fetch and print an agent card")
    card.add_argument("url", help="agent base URL")

    return parser


def _serve(agent: AgentName) -> int:
    settings = get_settings()
    bind_agent(agent.value)
    app = build_agent_app(agent, settings)
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port_for(agent),
        log_config=None,
        access_log=False,
    )
    return 0


async def _dev() -> int:
    """Run all four agents in one process — four servers, four ports, still real A2A.

    Convenience only: the agents talk to each other over HTTP exactly as they do
    when deployed as separate containers.
    """
    settings = get_settings()
    servers = []
    for agent in AgentName:
        app = build_agent_app(agent, settings)
        config = uvicorn.Config(
            app,
            host=settings.host,
            port=settings.port_for(agent),
            log_config=None,
            access_log=False,
        )
        servers.append(uvicorn.Server(config))

    bind_agent("dev")
    logger.info(
        "starting all agents",
        extra={"ports": ",".join(str(settings.port_for(a)) for a in AgentName)},
    )
    await asyncio.gather(*(server.serve() for server in servers))
    return 0


async def _ask(question: str, base_url: str) -> int:
    """Ask the coordinator and stream the collaboration to stdout."""
    bind_agent("client")
    settings = get_settings()

    async with httpx.AsyncClient(timeout=settings.peer_request_timeout_seconds) as http_client:
        try:
            card = await A2ACardResolver(
                httpx_client=http_client, base_url=base_url
            ).get_agent_card()
        except Exception as exc:  # noqa: BLE001 - surface a readable message, not a traceback
            print(f"Could not fetch the agent card from {base_url}: {exc}", file=sys.stderr)
            return 2

        print(f"→ {card.name} v{card.version} — {', '.join(s.id for s in card.skills)}\n")

        client = ClientFactory(ClientConfig(streaming=True, httpx_client=http_client)).create(card)
        request = a2a_pb2.SendMessageRequest(
            message=a2a_pb2.Message(
                message_id=f"cli-{id(question):x}",
                role=a2a_pb2.ROLE_USER,
                parts=[a2a_pb2.Part(text=question)],
            )
        )

        # The client is not closed explicitly: Client.close() disposes of the
        # httpx client, which the enclosing context manager already owns.
        final: a2a_pb2.Task | None = None
        try:
            async for chunk in client.send_message(request):
                final = _render(chunk) or final
        except Exception as exc:  # noqa: BLE001
            print(f"\nRequest failed: {exc}", file=sys.stderr)
            return 2

    return _render_final(final)


def _render(chunk: a2a_pb2.StreamResponse) -> a2a_pb2.Task | None:
    """Print one streamed event; return the task when the payload carries one."""
    payload = chunk.WhichOneof("payload")

    if payload == "status_update":
        status = chunk.status_update.status
        state = a2a_pb2.TaskState.Name(status.state).removeprefix("TASK_STATE_").lower()
        text = get_message_text(status.message) if status.HasField("message") else ""
        print(f"  [{state:<9}] {text}")
        return None

    if payload == "artifact_update":
        artifact = chunk.artifact_update.artifact
        print(f"  [artifact ] {artifact.name} ({len(get_artifact_text(artifact))} chars)")
        return None

    if payload == "task":
        return chunk.task

    if payload == "message":
        print(f"  [message  ] {get_message_text(chunk.message)}")
    return None


def _render_final(task: a2a_pb2.Task | None) -> int:
    if task is None:
        print("\nNo task was returned.", file=sys.stderr)
        return 2

    state = a2a_pb2.TaskState.Name(task.status.state)
    print(f"\n{'=' * 72}\ntask {task.id}  context {task.context_id}  state {state}\n{'=' * 72}")

    for artifact in task.artifacts:
        print(f"\n--- {artifact.name} ---\n")
        print(get_artifact_text(artifact))

    if not task.artifacts and task.status.HasField("message"):
        print(get_message_text(task.status.message))

    return 0 if state == "TASK_STATE_COMPLETED" else 1


async def _card(base_url: str) -> int:
    async with httpx.AsyncClient(timeout=30) as http_client:
        try:
            card = await A2ACardResolver(
                httpx_client=http_client, base_url=base_url
            ).get_agent_card()
        except Exception as exc:  # noqa: BLE001
            print(f"Could not fetch the agent card from {base_url}: {exc}", file=sys.stderr)
            return 2
    display_agent_card(card)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
