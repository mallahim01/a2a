"""The HTTP surface each agent exposes, exercised over the wire format."""

from __future__ import annotations

import pytest
from a2a.utils.constants import VERSION_HEADER

from research_desk.config import AgentName
from tests.a2a_helpers import RPC_HEADERS, fetch_card, rpc
from tests.conftest import Desk

ALL_AGENTS = list(AgentName)


@pytest.mark.parametrize("agent", ALL_AGENTS)
async def test_agent_card_is_served_at_the_well_known_path(desk: Desk, agent: AgentName) -> None:
    card = await fetch_card(desk.client, desk.url(agent))

    assert card["name"]
    assert card["skills"]
    assert card["supportedInterfaces"][0]["protocolVersion"] == "1.0"
    assert card["supportedInterfaces"][0]["protocolBinding"] == "JSONRPC"


@pytest.mark.parametrize("agent", ALL_AGENTS)
async def test_health_reports_identity_and_skills(desk: Desk, agent: AgentName) -> None:
    response = await desk.client.get(f"{desk.url(agent)}/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["protocol_version"] == "1.0"
    assert body["skills"]


async def test_send_message_returns_a_completed_task_with_an_artifact(desk: Desk) -> None:
    result = await rpc(
        desk.client,
        desk.url(AgentName.RESEARCHER),
        "SendMessage",
        {
            "message": {
                "messageId": "m-1",
                "role": "ROLE_USER",
                "parts": [{"text": "Open agent interoperability protocols"}],
            }
        },
    )

    task = result["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert task["id"] and task["contextId"]
    assert task["artifacts"][0]["name"] == "research-notes.md"
    assert task["artifacts"][0]["parts"][0]["text"]


async def test_a_completed_task_can_be_fetched_again_by_id(desk: Desk) -> None:
    url = desk.url(AgentName.WRITER)
    sent = await rpc(
        desk.client,
        url,
        "SendMessage",
        {"message": {"messageId": "m-2", "role": "ROLE_USER", "parts": [{"text": "notes here"}]}},
    )
    task_id = sent["result"]["task"]["id"]

    # SendMessage answers with a SendMessageResponse (a task-or-message union),
    # whereas GetTask answers with the Task itself.
    fetched = await rpc(desk.client, url, "GetTask", {"id": task_id}, request_id="2")

    assert fetched["result"]["id"] == task_id
    assert fetched["result"]["status"]["state"] == "TASK_STATE_COMPLETED"
    assert fetched["result"]["artifacts"][0]["name"] == "brief.md"


async def test_a_short_message_drives_the_task_to_input_required(desk: Desk) -> None:
    result = await rpc(
        desk.client,
        desk.url(AgentName.ANALYST),
        "SendMessage",
        {"message": {"messageId": "m-3", "role": "ROLE_USER", "parts": [{"text": "hi"}]}},
    )

    assert result["result"]["task"]["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"


async def test_unknown_methods_are_rejected(desk: Desk) -> None:
    result = await rpc(desk.client, desk.url(AgentName.WRITER), "Teleport", {})

    assert result["error"]["code"] == -32601


async def test_malformed_json_is_a_parse_error(desk: Desk) -> None:
    response = await desk.client.post(
        f"{desk.url(AgentName.WRITER)}/", headers=RPC_HEADERS, content=b"{not json"
    )

    assert response.json()["error"]["code"] == -32700


async def test_a_request_without_the_version_header_is_refused(desk: Desk) -> None:
    """A v1.0 server identifies the caller's protocol version from ``A2A-Version``.

    Omitting it makes the server assume a v0.3 client and decline, which is why
    every hand-written JSON-RPC example in the README sets the header.
    """
    result = await rpc(
        desk.client,
        desk.url(AgentName.WRITER),
        "SendMessage",
        {"message": {"messageId": "m-4", "role": "ROLE_USER", "parts": [{"text": "notes"}]}},
        headers={},
    )

    assert "error" in result
    assert "1.0" in result["error"]["message"]

    with_header = await rpc(
        desk.client,
        desk.url(AgentName.WRITER),
        "SendMessage",
        {"message": {"messageId": "m-4b", "role": "ROLE_USER", "parts": [{"text": "notes"}]}},
        headers={VERSION_HEADER: "1.0"},
    )
    assert "result" in with_header


async def test_the_coordinator_publishes_what_it_discovered(desk: Desk) -> None:
    # Discovery happens lazily on first use when the lifespan has not run.
    await rpc(
        desk.client,
        desk.url(AgentName.COORDINATOR),
        "SendMessage",
        {
            "message": {
                "messageId": "m-5",
                "role": "ROLE_USER",
                "parts": [{"text": "state of open agent interoperability protocols"}],
            }
        },
    )

    body = (await desk.client.get(f"{desk.url(AgentName.COORDINATOR)}/agents")).json()

    assert len(body["discovered"]) == 3
    assert set(body["skills"]) == {"gather_sources", "extract_insights", "compose_brief"}
    assert {agent["name"] for agent in body["discovered"]} == {"Researcher", "Analyst", "Writer"}
