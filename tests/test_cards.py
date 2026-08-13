"""Agent cards — the identity documents the whole system routes on."""

from __future__ import annotations

import pytest
from a2a.utils import TransportProtocol
from a2a.utils.constants import PROTOCOL_VERSION_CURRENT

from research_desk.cards import (
    SKILL_COMPOSE_BRIEF,
    SKILL_EXTRACT_INSIGHTS,
    SKILL_GATHER_SOURCES,
    SKILL_RESEARCH_BRIEF,
    build_card,
)
from research_desk.config import AgentName

EXPECTED_SKILLS = {
    AgentName.COORDINATOR: SKILL_RESEARCH_BRIEF,
    AgentName.RESEARCHER: SKILL_GATHER_SOURCES,
    AgentName.ANALYST: SKILL_EXTRACT_INSIGHTS,
    AgentName.WRITER: SKILL_COMPOSE_BRIEF,
}


@pytest.mark.parametrize("agent", list(AgentName))
def test_every_agent_publishes_a_complete_card(agent: AgentName) -> None:
    card = build_card(agent, "http://example.test:8000/")

    assert card.name
    assert card.description
    assert card.version
    assert card.provider.organization
    assert card.default_input_modes
    assert card.default_output_modes


@pytest.mark.parametrize("agent", list(AgentName))
def test_cards_advertise_the_current_protocol_over_jsonrpc(agent: AgentName) -> None:
    card = build_card(agent, "http://example.test:8000/")
    interface = card.supported_interfaces[0]

    assert interface.url == "http://example.test:8000/"
    assert interface.protocol_binding == TransportProtocol.JSONRPC
    assert interface.protocol_version == PROTOCOL_VERSION_CURRENT


@pytest.mark.parametrize(("agent", "skill_id"), list(EXPECTED_SKILLS.items()))
def test_each_agent_advertises_its_routing_skill(agent: AgentName, skill_id: str) -> None:
    card = build_card(agent, "http://example.test/")
    assert [skill.id for skill in card.skills] == [skill_id]


def test_skill_ids_are_unique_across_the_desk() -> None:
    ids = [skill.id for agent in AgentName for skill in build_card(agent, "http://x.test/").skills]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("agent", list(AgentName))
def test_skills_carry_the_metadata_a_discovering_agent_needs(agent: AgentName) -> None:
    for skill in build_card(agent, "http://x.test/").skills:
        assert skill.name and skill.description
        assert skill.tags
        assert skill.examples


@pytest.mark.parametrize("agent", list(AgentName))
def test_streaming_is_advertised(agent: AgentName) -> None:
    assert build_card(agent, "http://x.test/").capabilities.streaming is True
