"""API-key authentication, declared in the card and enforced at the endpoint."""

from __future__ import annotations

import pytest
from a2a.client import A2AClientError

from research_desk.cards import build_card
from research_desk.config import AgentName
from research_desk.protocol.auth import (
    API_KEY_HEADER,
    API_KEY_SCHEME,
    StaticApiKeyCredentials,
)
from tests.a2a_helpers import RPC_HEADERS, fetch_card, rpc, send_message, state_of
from tests.conftest import Desk, open_desk

KEY = "test-desk-key"
MESSAGE = {"messageId": "auth-1", "role": "ROLE_USER", "parts": [{"text": "notes to write up"}]}


@pytest.fixture
async def secured():
    async with open_desk(a2a_api_key=KEY) as desk:
        yield desk


# --- what the card advertises ------------------------------------------------


def test_an_open_agent_declares_no_security() -> None:
    card = build_card(AgentName.WRITER, "http://x.test/")

    assert not card.security_schemes
    assert not card.security_requirements


def test_a_secured_agent_declares_the_api_key_scheme() -> None:
    card = build_card(AgentName.WRITER, "http://x.test/", require_api_key=True)

    scheme = card.security_schemes[API_KEY_SCHEME]
    assert scheme.api_key_security_scheme.name == API_KEY_HEADER
    assert scheme.api_key_security_scheme.location == "header"
    assert API_KEY_SCHEME in card.security_requirements[0].schemes


async def test_the_published_card_tells_a_caller_how_to_authenticate(secured: Desk) -> None:
    """Discovery has to work unauthenticated, or nobody can learn the scheme."""
    card = await fetch_card(secured.client, secured.url(AgentName.WRITER))

    assert card["securitySchemes"][API_KEY_SCHEME]["apiKeySecurityScheme"]["name"] == API_KEY_HEADER
    assert card["securityRequirements"][0]["schemes"][API_KEY_SCHEME] == {}


# --- enforcement -------------------------------------------------------------


@pytest.mark.parametrize("path", ["/.well-known/agent-card.json", "/health"])
async def test_discovery_and_health_stay_public(secured: Desk, path: str) -> None:
    response = await secured.client.get(f"{secured.url(AgentName.WRITER)}{path}")

    assert response.status_code == 200


async def test_health_reports_that_auth_is_required(secured: Desk) -> None:
    body = (await secured.client.get(f"{secured.url(AgentName.WRITER)}/health")).json()

    assert body["auth_required"] is True


async def test_the_protocol_endpoint_rejects_a_missing_key(secured: Desk) -> None:
    response = await secured.client.post(
        f"{secured.url(AgentName.WRITER)}/",
        headers=RPC_HEADERS,
        json={"jsonrpc": "2.0", "id": "1", "method": "SendMessage", "params": {"message": MESSAGE}},
    )

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == API_KEY_HEADER
    assert API_KEY_HEADER in response.json()["error"]["message"]


async def test_the_protocol_endpoint_rejects_a_wrong_key(secured: Desk) -> None:
    response = await secured.client.post(
        f"{secured.url(AgentName.WRITER)}/",
        headers={**RPC_HEADERS, API_KEY_HEADER: "not-the-key"},
        json={"jsonrpc": "2.0", "id": "1", "method": "SendMessage", "params": {"message": MESSAGE}},
    )

    assert response.status_code == 401


async def test_a_correct_key_is_accepted(secured: Desk) -> None:
    result = await rpc(
        secured.client,
        secured.url(AgentName.WRITER),
        "SendMessage",
        {"message": MESSAGE},
        headers={**RPC_HEADERS, API_KEY_HEADER: KEY},
    )

    assert result["result"]["task"]["status"]["state"] == "TASK_STATE_COMPLETED"


async def test_an_open_desk_needs_no_key(desk: Desk) -> None:
    body = (await desk.client.get(f"{desk.url(AgentName.WRITER)}/health")).json()
    result = await rpc(desk.client, desk.url(AgentName.WRITER), "SendMessage", {"message": MESSAGE})

    assert body["auth_required"] is False
    assert result["result"]["task"]["status"]["state"] == "TASK_STATE_COMPLETED"


# --- the client side ---------------------------------------------------------


async def test_the_credential_service_answers_only_for_its_scheme() -> None:
    credentials = StaticApiKeyCredentials(KEY)

    assert await credentials.get_credentials(API_KEY_SCHEME, None) == KEY
    assert await credentials.get_credentials("oauth2", None) is None


async def test_agents_authenticate_to_each_other_end_to_end(secured: Desk) -> None:
    """The coordinator's delegated calls must carry the key, or every hop 401s."""
    card = build_card(
        AgentName.COORDINATOR,
        f"{secured.url(AgentName.COORDINATOR)}/",
        require_api_key=True,
    )

    task = await send_message(
        secured.client,
        card,
        "the state of open agent interoperability protocols",
        api_key=KEY,
    )

    assert state_of(task) == "TASK_STATE_COMPLETED"


async def test_a_client_without_the_key_cannot_reach_the_coordinator(secured: Desk) -> None:
    card = build_card(
        AgentName.COORDINATOR,
        f"{secured.url(AgentName.COORDINATOR)}/",
        require_api_key=True,
    )

    with pytest.raises(A2AClientError, match="401"):
        await send_message(secured.client, card, "a question with enough words")
