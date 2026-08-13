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
import json
import sys

import httpx
import uvicorn
from a2a.client import (
    A2ACardResolver,
    AuthInterceptor,
    ClientConfig,
    ClientFactory,
)
from a2a.helpers import (
    display_agent_card,
    get_artifact_text,
    get_data_parts,
    get_message_text,
)
from a2a.types import a2a_pb2

from research_desk import __version__
from research_desk.agents import build_agent_app
from research_desk.config import AgentName, get_settings
from research_desk.logging import bind_agent, configure_logging, get_logger
from research_desk.protocol.auth import StaticApiKeyCredentials

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

        # The interceptor reads the card's securitySchemes and attaches the
        # credential in whatever form the agent asked for.
        interceptors = (
            [AuthInterceptor(StaticApiKeyCredentials(settings.a2a_api_key))]
            if settings.a2a_api_key
            else None
        )
        client = ClientFactory(ClientConfig(streaming=True, httpx_client=http_client)).create(
            card, interceptors=interceptors
        )
        request = a2a_pb2.SendMessageRequest(
            message=a2a_pb2.Message(
                message_id=f"cli-{id(question):x}",
                role=a2a_pb2.ROLE_USER,
                parts=[a2a_pb2.Part(text=question)],
            )
        )

        # The client is not closed explicitly: Client.close() disposes of the
        # httpx client, which the enclosing context manager already owns.
        stream = TaskStream()
        try:
            async for chunk in client.send_message(request):
                stream.consume(chunk)
        except Exception as exc:  # noqa: BLE001
            print(f"\nRequest failed: {exc}", file=sys.stderr)
            advertised = card.supported_interfaces[0].url if card.supported_interfaces else ""
            if advertised and not advertised.startswith(base_url.rstrip("/")):
                print(
                    f"The agent card advertises {advertised}, which this machine may not be "
                    f"able to reach. Set PUBLIC_URL on that agent to an address its callers "
                    f"can resolve.",
                    file=sys.stderr,
                )
            return 2

    return stream.report()


class TaskStream:
    """Folds a stream of A2A events back into the task they describe.

    A streaming ``SendMessage`` opens with the freshly submitted ``Task`` and
    then sends deltas: ``TaskStatusUpdateEvent`` as the work progresses and
    ``TaskArtifactUpdateEvent`` as results are published. A client that keeps
    only the opening task sees it stuck at ``submitted``, so the deltas have to
    be applied as they arrive.
    """

    def __init__(self) -> None:
        self._task: a2a_pb2.Task | None = None
        self._artifacts: dict[str, a2a_pb2.Artifact] = {}

    def consume(self, chunk: a2a_pb2.StreamResponse) -> None:
        payload = chunk.WhichOneof("payload")

        if payload == "task":
            self._task = chunk.task
            for artifact in chunk.task.artifacts:
                self._artifacts[artifact.artifact_id] = artifact

        elif payload == "status_update":
            status = chunk.status_update.status
            if self._task is not None:
                self._task.status.CopyFrom(status)
            state = a2a_pb2.TaskState.Name(status.state).removeprefix("TASK_STATE_").lower()
            text = get_message_text(status.message) if status.HasField("message") else ""
            print(f"  [{state:<9}] {text}".rstrip())

        elif payload == "artifact_update":
            artifact = chunk.artifact_update.artifact
            self._artifacts[artifact.artifact_id] = artifact
            print(f"  [artifact ] {artifact.name} ({_describe(artifact)})")

        elif payload == "message":
            print(f"  [message  ] {get_message_text(chunk.message)}")

    def report(self) -> int:
        """Print the finished task. Returns a shell exit code."""
        if self._task is None:
            print("\nNo task was returned.", file=sys.stderr)
            return 2

        state = a2a_pb2.TaskState.Name(self._task.status.state)
        print(
            f"\n{'=' * 72}\n"
            f"task {self._task.id}  context {self._task.context_id}  state {state}\n"
            f"{'=' * 72}"
        )

        for artifact in self._artifacts.values():
            print(f"\n--- {artifact.name} ---\n")
            print(_render_artifact(artifact))

        if not self._artifacts and self._task.status.HasField("message"):
            print(get_message_text(self._task.status.message))

        return 0 if state == "TASK_STATE_COMPLETED" else 1


def _describe(artifact: a2a_pb2.Artifact) -> str:
    if text := get_artifact_text(artifact):
        return f"{len(text)} chars"
    return f"{len(artifact.parts)} data part(s)"


def _render_artifact(artifact: a2a_pb2.Artifact) -> str:
    if text := get_artifact_text(artifact):
        return text
    return "\n".join(json.dumps(data, indent=2) for data in get_data_parts(artifact.parts))


async def _card(base_url: str) -> int:
    async with httpx.AsyncClient(timeout=30) as http_client:
        try:
            card = await A2ACardResolver(
                httpx_client=http_client, base_url=base_url
            ).get_agent_card()
        except Exception as exc:  # noqa: BLE001
            print(f"Could not fetch the agent card from {base_url}: {exc}", file=sys.stderr)
            return 2
    # Agent cards stay public even when the protocol endpoint is guarded — a
    # caller has to read the card to learn which credential to present.
    display_agent_card(card)
    if card.security_requirements:
        schemes = ", ".join(sorted(card.security_schemes))
        print(f"\nThis agent requires authentication. Schemes: {schemes}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
