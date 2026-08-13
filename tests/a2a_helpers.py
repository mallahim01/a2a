"""Helpers for driving agents over A2A from tests."""

from __future__ import annotations

from typing import Any

import httpx
from a2a.client import ClientConfig, ClientFactory
from a2a.helpers import get_artifact_text, get_data_parts
from a2a.types import a2a_pb2
from a2a.utils.constants import VERSION_HEADER

PROTOCOL_VERSION = "1.0"

#: Headers a hand-written JSON-RPC caller must send. The version header is how a
#: v1.0 server distinguishes a v1.0 client from a v0.3 one.
RPC_HEADERS = {VERSION_HEADER: PROTOCOL_VERSION}


async def fetch_card(client: httpx.AsyncClient, base_url: str) -> dict[str, Any]:
    response = await client.get(f"{base_url}/.well-known/agent-card.json")
    response.raise_for_status()
    return dict(response.json())


async def rpc(
    client: httpx.AsyncClient,
    base_url: str,
    method: str,
    params: dict[str, Any],
    *,
    request_id: str = "1",
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Issue a raw JSON-RPC call, bypassing the SDK client."""
    response = await client.post(
        f"{base_url}/",
        headers=RPC_HEADERS if headers is None else headers,
        json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
    )
    return dict(response.json())


async def send_message(
    client: httpx.AsyncClient,
    card: a2a_pb2.AgentCard,
    text: str,
    *,
    task_id: str = "",
    context_id: str = "",
) -> a2a_pb2.Task:
    """Send a message with the SDK client and return the final task."""
    a2a_client = ClientFactory(ClientConfig(streaming=False, httpx_client=client)).create(card)
    message = a2a_pb2.Message(
        message_id=f"test-{abs(hash((text, task_id))):x}",
        role=a2a_pb2.ROLE_USER,
        parts=[a2a_pb2.Part(text=text)],
    )
    if task_id:
        message.task_id = task_id
    if context_id:
        message.context_id = context_id

    # Not closed on purpose: Client.close() would tear down the shared httpx
    # client that the whole desk is wired to.
    task: a2a_pb2.Task | None = None
    async for chunk in a2a_client.send_message(a2a_pb2.SendMessageRequest(message=message)):
        if chunk.WhichOneof("payload") == "task":
            task = chunk.task

    assert task is not None, "agent returned no task"
    return task


def artifact(task: a2a_pb2.Task, name: str) -> a2a_pb2.Artifact:
    for candidate in task.artifacts:
        if candidate.name == name:
            return candidate
    raise AssertionError(f"no artifact named {name!r}; got {[a.name for a in task.artifacts]}")


def artifact_text(task: a2a_pb2.Task, name: str) -> str:
    return get_artifact_text(artifact(task, name))


def artifact_data(task: a2a_pb2.Task, name: str) -> dict[str, Any]:
    parts = get_data_parts(artifact(task, name).parts)
    assert parts, f"artifact {name!r} carries no data part"
    return dict(parts[0])


def state_of(task: a2a_pb2.Task) -> str:
    return a2a_pb2.TaskState.Name(task.status.state)
