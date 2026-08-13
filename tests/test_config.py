"""Configuration loading and the key-pool logic providers depend on."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from research_desk.config import AgentName, Settings, collect_api_keys, load_env_file


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


def test_env_file_populates_settings_and_the_key_pool(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "GROQ_API_KEY=from-file\nGROQ_API_KEY_2=second\nWRITER_MODEL=groq:from-file\n",
        encoding="utf-8",
    )

    assert load_env_file(env_file) is True
    assert Settings().writer_model == "groq:from-file"
    assert Settings().groq_api_keys == ["from-file", "second"]


def test_real_environment_variables_beat_the_env_file(tmp_path: Path) -> None:
    """Containers inject secrets as real env vars; a stray file must not win."""
    os.environ["GROQ_API_KEY"] = "from-environment"
    env_file = tmp_path / ".env"
    env_file.write_text("GROQ_API_KEY=from-file\n", encoding="utf-8")

    load_env_file(env_file)

    assert Settings().groq_api_keys == ["from-environment"]


def test_a_missing_env_file_is_not_an_error(tmp_path: Path) -> None:
    assert load_env_file(tmp_path / "absent.env") is False


def test_peer_urls_parse_from_a_comma_separated_string() -> None:
    settings = Settings(peer_agent_urls="http://a:1/, http://b:2 ")
    assert settings.peer_agent_urls == ["http://a:1", "http://b:2"]


def test_peer_urls_parse_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The env var is a plain comma-separated list, not JSON.

    Worth testing separately: passing the value to ``Settings(...)`` directly
    bypasses the environment source that does the decoding.
    """
    monkeypatch.setenv("PEER_AGENT_URLS", "http://researcher:8001,http://analyst:8002")

    assert Settings().peer_agent_urls == ["http://researcher:8001", "http://analyst:8002"]


def test_blank_environment_variables_fall_back_to_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``PORT=`` in a compose file means "unset", not "the empty string"."""
    monkeypatch.setenv("PORT", "")
    monkeypatch.setenv("WRITER_MODEL", "   ")
    monkeypatch.setenv("PEER_AGENT_URLS", "")

    settings = Settings()

    assert settings.port is None
    assert settings.port_for(AgentName.WRITER) == 8003
    assert settings.writer_model == "groq:llama-3.3-70b-versatile"
    assert settings.peer_agent_urls == [
        "http://127.0.0.1:8001",
        "http://127.0.0.1:8002",
        "http://127.0.0.1:8003",
    ]


def test_settings_resolve_entirely_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The container path: every value arrives as an environment variable."""
    monkeypatch.setenv("PORT", "9100")
    monkeypatch.setenv("PUBLIC_URL", "http://analyst:9100/")
    monkeypatch.setenv("ANALYST_MODEL", "groq:some-model")
    monkeypatch.setenv("RESEARCHER_ENABLE_SEARCH", "true")
    monkeypatch.setenv("LOG_FORMAT", "json")

    settings = Settings()

    assert settings.port_for(AgentName.ANALYST) == 9100
    assert settings.public_url_for(AgentName.ANALYST) == "http://analyst:9100/"
    assert settings.model_for(AgentName.ANALYST) == "groq:some-model"
    assert settings.researcher_enable_search is True
    assert settings.log_format == "json"


def test_port_prefers_explicit_over_agent_default() -> None:
    assert Settings().port_for(AgentName.ANALYST) == 8002
    assert Settings(port=9999).port_for(AgentName.ANALYST) == 9999


def test_public_url_rewrites_wildcard_bind_to_a_reachable_host() -> None:
    settings = Settings(host="0.0.0.0")  # noqa: S104
    assert settings.public_url_for(AgentName.WRITER) == "http://127.0.0.1:8003/"


def test_public_url_override_is_normalised_with_a_trailing_slash() -> None:
    settings = Settings(public_url="http://writer:8003")
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
        coordinator_model="stub:c",
        researcher_model="stub:r",
        analyst_model="stub:a",
        writer_model="stub:w",
    )
    assert settings.model_for(agent) == expected
