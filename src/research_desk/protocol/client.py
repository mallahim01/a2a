"""The outbound half of the protocol: calling another agent over A2A.

An agent that delegates is simply an A2A *client* of another A2A *server*. The
coordinator uses this module for every hand-off; it never imports a specialist's
code, and the only thing it knows about a peer is that peer's agent card.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import httpx
from a2a.client import Client, ClientConfig, ClientFactory
from a2a.helpers import get_artifact_text, get_data_parts, get_message_text
from a2a.types import a2a_pb2

from research_desk.logging import get_logger

logger = get_logger(__name__)


class PeerError(RuntimeError):
    """A delegated call did not produce a usable result."""


@dataclass(slots=True)
class PeerResult:
    """What came back from one delegated A2A task."""

    agent_name: str
    skill_id: str
    task_id: str
    context_id: str
    state: str
    text: str
    data: list[Any] = field(default_factory=list)
    duration_ms: int = 0

    def as_trace(self) -> dict[str, Any]:
        """Compact record of the hop, published as part of the coordinator's trace."""
        return {
            "agent": self.agent_name,
            "skill": self.skill_id,
            "task_id": self.task_id,
            "context_id": self.context_id,
            "state": self.state,
            "duration_ms": self.duration_ms,
            "characters_returned": len(self.text),
        }


class PeerClient:
    """Sends A2A messages to peers discovered at runtime.

    The httpx client is injected so the same code path runs over a real network
    in production and over an in-process ASGI transport in the test suite. It is
    also shared by every peer connection, which is why the SDK clients cached
    here are never closed individually — ``Client.close()`` would tear down the
    shared transport. The application that owns the httpx client closes it.
    """

    def __init__(self, http_client: httpx.AsyncClient) -> None:
        self._factory = ClientFactory(ClientConfig(streaming=False, httpx_client=http_client))
        self._clients: dict[str, Client] = {}

    def _client_for(self, card: a2a_pb2.AgentCard) -> Client:
        key = card.supported_interfaces[0].url if card.supported_interfaces else card.name
        if key not in self._clients:
            self._clients[key] = self._factory.create(card)
        return self._clients[key]

    async def delegate(
        self,
        *,
        card: a2a_pb2.AgentCard,
        skill_id: str,
        prompt: str,
        context_id: str = "",
    ) -> PeerResult:
        """Run one skill on one peer and wait for the task to reach a final state.

        ``context_id`` is propagated so that every agent involved in a request logs
        the same correlation id — the thread that ties a distributed run together.
        """
        started = time.perf_counter()
        client = self._client_for(card)

        message = a2a_pb2.Message(
            message_id=f"{skill_id}-{uuid4()}",
            role=a2a_pb2.ROLE_USER,
            parts=[a2a_pb2.Part(text=prompt)],
        )
        if context_id:
            message.context_id = context_id

        logger.info(
            "delegating over a2a",
            extra={"peer": card.name, "skill": skill_id, "prompt_chars": len(prompt)},
        )

        try:
            task = await self._send(client, a2a_pb2.SendMessageRequest(message=message))
        except PeerError:
            raise
        except Exception as exc:
            # Any transport-level failure is reported as a peer failure so the
            # coordinator can decide whether the pipeline survives without it.
            raise PeerError(f"{card.name} could not be reached: {exc}") from exc

        result = _to_result(task, agent_name=card.name, skill_id=skill_id)
        result.duration_ms = int((time.perf_counter() - started) * 1000)

        if task.status.state != a2a_pb2.TASK_STATE_COMPLETED:
            raise PeerError(
                f"{card.name} finished in state {result.state}"
                + (f": {result.text}" if result.text else "")
            )
        if not result.text.strip():
            raise PeerError(f"{card.name} completed without returning any content")

        logger.info(
            "peer completed",
            extra={
                "peer": card.name,
                "skill": skill_id,
                "state": result.state,
                "duration_ms": result.duration_ms,
            },
        )
        return result

    @staticmethod
    async def _send(client: Client, request: a2a_pb2.SendMessageRequest) -> a2a_pb2.Task:
        """Drain the response stream and return the final Task."""
        task: a2a_pb2.Task | None = None
        message: a2a_pb2.Message | None = None

        async for chunk in client.send_message(request):
            payload = chunk.WhichOneof("payload")
            if payload == "task":
                task = chunk.task
            elif payload == "message":
                message = chunk.message

        if task is None:
            if message is not None:
                raise PeerError(
                    "peer replied with a bare message instead of a task: "
                    f"{get_message_text(message)[:200]}"
                )
            raise PeerError("peer returned no task")
        return task


def _to_result(task: a2a_pb2.Task, *, agent_name: str, skill_id: str) -> PeerResult:
    texts: list[str] = []
    data: list[Any] = []
    for artifact in task.artifacts:
        if text := get_artifact_text(artifact):
            texts.append(text)
        data.extend(get_data_parts(artifact.parts))

    # A failing peer explains itself in the status message rather than an artifact.
    if not texts and task.status.HasField("message"):
        texts.append(get_message_text(task.status.message))

    return PeerResult(
        agent_name=agent_name,
        skill_id=skill_id,
        task_id=task.id,
        context_id=task.context_id,
        state=a2a_pb2.TaskState.Name(task.status.state),
        text="\n\n".join(texts).strip(),
        data=data,
    )
