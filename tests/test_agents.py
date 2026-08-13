"""Specialist agent behaviour and task lifecycle, driven through the protocol."""

from __future__ import annotations

import json

import pytest
from a2a.types import a2a_pb2

from research_desk.agents.analyst import AnalystExecutor
from research_desk.agents.researcher import ResearcherExecutor
from research_desk.agents.writer import WriterExecutor
from research_desk.cards import build_card
from research_desk.config import AgentName
from research_desk.llm import ChatRequest, LLMError, StubProvider
from research_desk.protocol.server import build_app
from tests.a2a_helpers import artifact, artifact_data, artifact_text, send_message, state_of
from tests.conftest import Desk

SPECIALISTS = [
    (AgentName.RESEARCHER, "research-notes.md"),
    (AgentName.ANALYST, "findings.json"),
    (AgentName.WRITER, "brief.md"),
]


class FailingProvider:
    """A provider whose model call always fails, to test the error path."""

    name = "stub:always-fails"

    async def complete(self, request: ChatRequest) -> str:
        raise LLMError("upstream is on fire")

    async def aclose(self) -> None:
        return None


class RecordingProvider(StubProvider):
    """Stub that keeps the prompts it was given."""

    def __init__(self) -> None:
        super().__init__("recording")
        self.requests: list[ChatRequest] = []

    async def complete(self, request: ChatRequest) -> str:
        self.requests.append(request)
        return await super().complete(request)


def _client(desk: Desk, agent: AgentName) -> tuple[Desk, a2a_pb2.AgentCard]:
    return desk, build_card(agent, f"{desk.url(agent)}/")


@pytest.mark.parametrize(("agent", "artifact_name"), SPECIALISTS)
async def test_a_specialist_completes_and_publishes_its_artifact(
    desk: Desk, agent: AgentName, artifact_name: str
) -> None:
    _, card = _client(desk, agent)

    task = await send_message(desk.client, card, "Open agent interoperability protocols")

    assert state_of(task) == "TASK_STATE_COMPLETED"
    assert artifact_text(task, artifact_name).strip()


@pytest.mark.parametrize(("agent", "artifact_name"), SPECIALISTS)
async def test_artifacts_record_which_agent_and_model_produced_them(
    desk: Desk, agent: AgentName, artifact_name: str
) -> None:
    _, card = _client(desk, agent)

    task = await send_message(desk.client, card, "Open agent interoperability protocols")
    metadata = dict(artifact(task, artifact_name).metadata)

    assert metadata["produced_by"] == card.name
    assert metadata["model"].startswith("stub:")


async def test_the_analyst_publishes_findings_as_structured_data(desk: Desk) -> None:
    _, card = _client(desk, AgentName.ANALYST)

    task = await send_message(desk.client, card, "Some research notes about agent protocols")
    findings = artifact_data(task, "findings.json")

    assert set(findings) >= {"themes", "risks", "open_questions"}
    # The same content is also readable as text for humans.
    assert json.loads(artifact_text(task, "findings.json"))["themes"] == findings["themes"]


@pytest.mark.parametrize("agent", [a for a, _ in SPECIALISTS])
async def test_a_too_short_message_asks_for_more_input(desk: Desk, agent: AgentName) -> None:
    _, card = _client(desk, agent)

    task = await send_message(desk.client, card, "hm")

    assert state_of(task) == "TASK_STATE_INPUT_REQUIRED"
    assert not task.artifacts


async def test_task_and_context_ids_are_assigned(desk: Desk) -> None:
    _, card = _client(desk, AgentName.WRITER)

    task = await send_message(desk.client, card, "notes to write up")

    assert task.id
    assert task.context_id


async def test_a_caller_supplied_context_id_is_honoured(desk: Desk) -> None:
    """Context propagation is what makes one collaboration traceable across agents."""
    _, card = _client(desk, AgentName.WRITER)

    task = await send_message(desk.client, card, "notes to write up", context_id="shared-ctx")

    assert task.context_id == "shared-ctx"


@pytest.mark.parametrize("executor_class", [ResearcherExecutor, AnalystExecutor, WriterExecutor])
async def test_a_model_failure_becomes_a_failed_task_not_a_crash(
    desk: Desk, executor_class: type
) -> None:
    card = build_card(AgentName.WRITER, "http://failing.test:8003/")
    app = build_app(
        card=card,
        executor=executor_class(FailingProvider(), agent_label="Failing"),  # type: ignore[arg-type]
    )

    import httpx

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), timeout=30) as client:
        task = await send_message(client, card, "a topic worth researching")

    assert state_of(task) == "TASK_STATE_FAILED"
    assert "upstream is on fire" in task.status.message.parts[0].text


async def test_each_specialist_frames_the_prompt_for_its_own_role() -> None:
    """The role-specific framing is what makes these separate agents, not one."""
    provider = RecordingProvider()
    researcher = ResearcherExecutor(provider, agent_label="Researcher")

    prompt = researcher.build_user_prompt("Agent protocols")

    assert prompt.startswith("Topic to research:")
    assert "Agent protocols" in prompt
    assert "## Current state" in researcher.system_prompt


async def test_the_analyst_requests_json_output() -> None:
    assert AnalystExecutor.json_output is True
    assert ResearcherExecutor.json_output is False
    assert WriterExecutor.json_output is False
