"""Configuration loading and the key-pool logic providers depend on."""

from __future__ import annotations

import pytest

from research_desk.config import AgentName, Settings, collect_api_keys


def test_collects_numbered_keys_in_order() -> None:
    env = {"GROQ_API_KEY": "a", "GROQ_API_KEY_2": "b", "GROQ_API_KEY_3": "c"}
    assert collect_api_keys("GROQ_API_KEY", env) == ["a", "b", "c"]


def test_ignores_blank_keys_but_keeps_scanning() -> None:
    env = {"GROQ_API_KEY": "a", "GROQ_API_KEY_2": "   ", "GROQ_API_KEY_3": "c"}
    assert collect_api_keys("GROQ_API_KEY", env) == ["a", "c"]


def test_stops_at_the_first_missing_index() -> None:
    env = {"GROQ_API_KEY": "a", "GROQ_API_KEY_3": "c"}
    assert collect_api_keys("GROQ_API_KEY", env) == ["a"]


def test_no_keys_configured() -> None:
    assert collect_api_keys("GROQ_API_KEY", {}) == []


def test_peer_urls_parse_from_a_comma_separated_string() -> None:
    settings = Settings(_env_file=None, peer_agent_urls="http://a:1/, http://b:2 ")
    assert settings.peer_agent_urls == ["http://a:1", "http://b:2"]


def test_port_prefers_explicit_over_agent_default() -> None:
    assert Settings(_env_file=None).port_for(AgentName.ANALYST) == 8002
    assert Settings(_env_file=None, port=9999).port_for(AgentName.ANALYST) == 9999


def test_public_url_rewrites_wildcard_bind_to_a_reachable_host() -> None:
    settings = Settings(_env_file=None, host="0.0.0.0")  # noqa: S104
    assert settings.public_url_for(AgentName.WRITER) == "http://127.0.0.1:8003/"


def test_public_url_override_is_normalised_with_a_trailing_slash() -> None:
    settings = Settings(_env_file=None, public_url="http://writer:8003")
    assert settings.public_url_for(AgentName.WRITER) == "http://writer:8003/"


@pytest.mark.parametrize(
    ("agent", "expected"),
    [
        (AgentName.COORDINATOR, "stub:c"),
        (AgentName.RESEARCHER, "stub:r"),
        (AgentName.ANALYST, "stub:a"),
        (AgentName.WRITER, "stub:w"),
    ],
)
def test_each_agent_reads_its_own_model_setting(agent: AgentName, expected: str) -> None:
    settings = Settings(
        _env_file=None,
        coordinator_model="stub:c",
        researcher_model="stub:r",
        analyst_model="stub:a",
        writer_model="stub:w",
    )
    assert settings.model_for(agent) == expected
