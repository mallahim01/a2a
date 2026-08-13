"""Agent discovery: fetching peer cards and routing on advertised skills."""

from __future__ import annotations

import httpx
import pytest
import respx

from research_desk.cards import build_card
from research_desk.config import AgentName
from research_desk.protocol.discovery import AgentRegistry, SkillNotAvailableError

RESEARCHER_URL = "http://researcher.test:8001"
ANALYST_URL = "http://analyst.test:8002"
CARD_PATH = "/.well-known/agent-card.json"


def card_json(agent: AgentName, url: str) -> dict[str, object]:
    from a2a.server.request_handlers.response_helpers import agent_card_to_dict

    return agent_card_to_dict(build_card(agent, f"{url}/"))


def mock_card(url: str, agent: AgentName) -> None:
    respx.get(f"{url}{CARD_PATH}").mock(
        return_value=httpx.Response(200, json=card_json(agent, url))
    )


async def build_registry(urls: list[str], **kwargs: object) -> AgentRegistry:
    async with httpx.AsyncClient() as client:
        registry = AgentRegistry(urls, client, retry_delay_seconds=0.0, **kwargs)  # type: ignore[arg-type]
        await registry.discover()
    return registry


@respx.mock
async def test_discovers_peers_and_indexes_their_skills() -> None:
    mock_card(RESEARCHER_URL, AgentName.RESEARCHER)
    mock_card(ANALYST_URL, AgentName.ANALYST)

    registry = await build_registry([RESEARCHER_URL, ANALYST_URL])

    assert {agent.name for agent in registry.agents} == {"Researcher", "Analyst"}
    assert registry.skill_ids == ["extract_insights", "gather_sources"]
    assert registry.by_skill("gather_sources").name == "Researcher"


@respx.mock
async def test_trailing_slashes_in_configuration_are_tolerated() -> None:
    mock_card(RESEARCHER_URL, AgentName.RESEARCHER)

    registry = await build_registry([f"{RESEARCHER_URL}/"])

    assert registry.has_skill("gather_sources")


@respx.mock
async def test_an_unreachable_peer_is_skipped_rather_than_fatal() -> None:
    mock_card(RESEARCHER_URL, AgentName.RESEARCHER)
    respx.get(f"{ANALYST_URL}{CARD_PATH}").mock(side_effect=httpx.ConnectError("refused"))

    registry = await build_registry([RESEARCHER_URL, ANALYST_URL], retries=1)

    assert [agent.name for agent in registry.agents] == ["Researcher"]
    assert not registry.has_skill("extract_insights")


@respx.mock
async def test_discovery_retries_a_peer_that_is_still_booting() -> None:
    route = respx.get(f"{RESEARCHER_URL}{CARD_PATH}").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(200, json=card_json(AgentName.RESEARCHER, RESEARCHER_URL)),
        ]
    )

    registry = await build_registry([RESEARCHER_URL], retries=3)

    assert route.call_count == 2
    assert registry.has_skill("gather_sources")


@respx.mock
async def test_requesting_an_undiscovered_skill_says_what_is_available() -> None:
    mock_card(RESEARCHER_URL, AgentName.RESEARCHER)
    registry = await build_registry([RESEARCHER_URL])

    with pytest.raises(SkillNotAvailableError, match="gather_sources"):
        registry.by_skill("compose_brief")


@respx.mock
async def test_the_first_agent_to_claim_a_skill_wins() -> None:
    twin_url = "http://twin.test:9001"
    mock_card(RESEARCHER_URL, AgentName.RESEARCHER)
    respx.get(f"{twin_url}{CARD_PATH}").mock(
        return_value=httpx.Response(200, json=card_json(AgentName.RESEARCHER, twin_url))
    )

    registry = await build_registry([RESEARCHER_URL, twin_url])

    assert len(registry.agents) == 2
    assert registry.by_skill("gather_sources").base_url == RESEARCHER_URL


@respx.mock
async def test_describe_exposes_the_routing_information_behind_a_decision() -> None:
    mock_card(RESEARCHER_URL, AgentName.RESEARCHER)
    registry = await build_registry([RESEARCHER_URL])

    described = registry.agents[0].describe()

    assert described["name"] == "Researcher"
    assert described["interfaces"][0]["protocol_version"] == "1.0"
    assert described["capabilities"]["streaming"] is True
    assert described["skills"][0]["id"] == "gather_sources"
