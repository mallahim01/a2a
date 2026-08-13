"""End-to-end: one question, four agents, real A2A traffic between them.

Every agent here is a separate ASGI application with its own agent card, task
store and executor. The coordinator finds the others by fetching their cards and
reaches them only through JSON-RPC ``SendMessage`` calls. Nothing in this test
imports a specialist from the coordinator's side of the boundary.
"""

from __future__ import annotations

from a2a.types import a2a_pb2

from research_desk.cards import build_card
from research_desk.config import AgentName
from tests.a2a_helpers import artifact_data, artifact_text, send_message, state_of
from tests.conftest import Desk, agent_url, open_desk

QUESTION = "the state of open agent interoperability protocols"


def coordinator_card(desk: Desk) -> a2a_pb2.AgentCard:
    return build_card(AgentName.COORDINATOR, f"{desk.url(AgentName.COORDINATOR)}/")


async def ask(desk: Desk, question: str = QUESTION) -> a2a_pb2.Task:
    return await send_message(desk.client, coordinator_card(desk), question)


async def test_the_full_pipeline_produces_a_brief(desk: Desk) -> None:
    task = await ask(desk)

    assert state_of(task) == "TASK_STATE_COMPLETED"
    assert artifact_text(task, "brief.md").strip()


async def test_the_response_records_every_a2a_hop(desk: Desk) -> None:
    task = await ask(desk)
    trace = artifact_data(task, "collaboration.json")

    assert trace["question"] == QUESTION
    assert trace["protocol"] == "A2A JSON-RPC (SendMessage)"
    assert [hop["skill"] for hop in trace["hops"]] == [
        "gather_sources",
        "extract_insights",
        "compose_brief",
    ]
    assert [hop["agent"] for hop in trace["hops"]] == ["Researcher", "Analyst", "Writer"]
    assert not trace["degradations"]


async def test_every_hop_reports_a_completed_peer_task(desk: Desk) -> None:
    trace = artifact_data(await ask(desk), "collaboration.json")

    for hop in trace["hops"]:
        assert hop["state"] == "TASK_STATE_COMPLETED"
        assert hop["task_id"]
        assert hop["characters_returned"] > 0


async def test_one_context_id_threads_through_all_four_agents(desk: Desk) -> None:
    """The coordinator propagates its context id, so a run is greppable end to end."""
    task = await ask(desk)
    trace = artifact_data(task, "collaboration.json")

    assert task.context_id
    assert {hop["context_id"] for hop in trace["hops"]} == {task.context_id}


async def test_the_coordinator_names_the_agents_it_collaborated_with(desk: Desk) -> None:
    trace = artifact_data(await ask(desk), "collaboration.json")

    assert set(trace["participants"]) == {"Researcher", "Analyst", "Writer"}


async def test_a_vague_question_asks_for_more_before_delegating(desk: Desk) -> None:
    task = await ask(desk, "protocols")

    assert state_of(task) == "TASK_STATE_INPUT_REQUIRED"
    assert not task.artifacts
    assert "too short" in task.status.message.parts[0].text


async def test_a_resumed_task_continues_and_completes(desk: Desk) -> None:
    """The ``input-required`` branch is resumable on the same task and context."""
    stalled = await ask(desk, "protocols")
    assert state_of(stalled) == "TASK_STATE_INPUT_REQUIRED"

    resumed = await send_message(
        desk.client,
        coordinator_card(desk),
        QUESTION,
        task_id=stalled.id,
        context_id=stalled.context_id,
    )

    assert resumed.id == stalled.id
    assert state_of(resumed) == "TASK_STATE_COMPLETED"
    assert artifact_text(resumed, "brief.md").strip()


async def test_the_brief_survives_the_analyst_going_missing() -> None:
    async with open_desk(offline={AgentName.ANALYST}) as desk:
        task = await ask(desk)
        trace = artifact_data(task, "collaboration.json")

    assert state_of(task) == "TASK_STATE_COMPLETED"
    assert [hop["skill"] for hop in trace["hops"]] == ["gather_sources", "compose_brief"]
    assert any("extract_insights" in note for note in trace["degradations"])


async def test_the_coordinator_writes_the_brief_itself_when_the_writer_is_missing() -> None:
    async with open_desk(offline={AgentName.WRITER}) as desk:
        task = await ask(desk)
        brief = artifact_text(task, "brief.md")

    assert state_of(task) == "TASK_STATE_COMPLETED"
    assert "writer agent was unavailable" in brief


async def test_losing_the_researcher_fails_the_task_with_an_explanation() -> None:
    async with open_desk(offline={AgentName.RESEARCHER}) as desk:
        task = await ask(desk)

    assert state_of(task) == "TASK_STATE_FAILED"
    assert "Research stage failed" in task.status.message.parts[0].text


async def test_no_peers_at_all_is_reported_clearly() -> None:
    async with open_desk(
        offline={AgentName.RESEARCHER, AgentName.ANALYST, AgentName.WRITER}
    ) as desk:
        task = await ask(desk)

    assert state_of(task) == "TASK_STATE_FAILED"
    assert "No specialist agents could be discovered" in task.status.message.parts[0].text


async def test_the_coordinator_routes_by_skill_not_by_address() -> None:
    """Move a skill to a different agent and the coordinator follows it.

    Here the writer is offline and the researcher's URL is the only one left in
    the peer list, so routing must fall out of the discovered cards.
    """
    async with open_desk(
        offline={AgentName.WRITER},
        peer_agent_urls=[agent_url(AgentName.ANALYST), agent_url(AgentName.RESEARCHER)],
    ) as desk:
        trace = artifact_data(await ask(desk), "collaboration.json")

    assert [hop["agent"] for hop in trace["hops"]] == ["Researcher", "Analyst"]


async def test_streaming_exposes_the_collaboration_as_it_happens(desk: Desk) -> None:
    """A streaming client sees each delegation as a status update, live."""
    from a2a.client import ClientConfig, ClientFactory
    from a2a.helpers import get_message_text

    client = ClientFactory(ClientConfig(streaming=True, httpx_client=desk.client)).create(
        coordinator_card(desk)
    )

    updates: list[str] = []
    artifacts: list[str] = []
    async for chunk in client.send_message(
        a2a_pb2.SendMessageRequest(
            message=a2a_pb2.Message(
                message_id="stream-1",
                role=a2a_pb2.ROLE_USER,
                parts=[a2a_pb2.Part(text=QUESTION)],
            )
        )
    ):
        payload = chunk.WhichOneof("payload")
        if payload == "status_update" and chunk.status_update.status.HasField("message"):
            updates.append(get_message_text(chunk.status_update.status.message))
        elif payload == "artifact_update":
            artifacts.append(chunk.artifact_update.artifact.name)

    assert any("Discovering specialist agents" in u for u in updates)
    assert any("Delegating 'gather_sources' to Researcher" in u for u in updates)
    assert any("Delegating 'compose_brief' to Writer" in u for u in updates)
    assert set(artifacts) == {"brief.md", "collaboration.json"}
