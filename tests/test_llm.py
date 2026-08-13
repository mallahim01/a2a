"""LLM provider behaviour: request shape, response parsing and key failover."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from research_desk.config import AgentName, Settings
from research_desk.llm import (
    ChatRequest,
    GeminiProvider,
    GroqProvider,
    LLMConfigurationError,
    LLMError,
    StubProvider,
    build_provider,
    build_provider_for,
    parse_model_spec,
)
from research_desk.llm.gemini import API_ROOT
from research_desk.llm.groq import API_URL as GROQ_URL

GEMINI_URL = f"{API_ROOT}/gemini-test:generateContent"

REQUEST = ChatRequest(system="be terse", user="hello")


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the retry back-off so the failover tests stay fast."""

    async def instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr("research_desk.llm._http.asyncio.sleep", instant)


def _groq_ok(content: str = "hi there") -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def _gemini_ok(text: str = "hi there") -> httpx.Response:
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": text}]}}]})


# --- model spec parsing ------------------------------------------------------


def test_parses_provider_and_model() -> None:
    assert parse_model_spec("groq:llama-3.3-70b-versatile") == ("groq", "llama-3.3-70b-versatile")


def test_model_names_may_contain_slashes() -> None:
    assert parse_model_spec("groq:openai/gpt-oss-120b") == ("groq", "openai/gpt-oss-120b")


@pytest.mark.parametrize("spec", ["", "groq", "groq:", ":model", "openai:gpt-4"])
def test_rejects_unusable_specs(spec: str) -> None:
    with pytest.raises(LLMConfigurationError):
        parse_model_spec(spec)


def test_missing_credentials_are_reported_at_construction() -> None:
    settings = Settings()
    with pytest.raises(LLMConfigurationError, match="No API keys configured"):
        build_provider("groq:llama-3.1-8b-instant", settings)


def test_stub_needs_no_credentials() -> None:
    provider = build_provider("stub:offline", Settings())
    assert provider.name == "stub:offline"


def test_search_grounding_is_only_wired_into_the_researcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "k")
    settings = Settings(
        researcher_enable_search=True,
        researcher_model="gemini:gemini-test",
        writer_model="gemini:gemini-test",
    )
    assert build_provider_for(AgentName.RESEARCHER, settings).name.endswith("+search")
    assert not build_provider_for(AgentName.WRITER, settings).name.endswith("+search")


# --- Groq --------------------------------------------------------------------


@respx.mock
async def test_groq_sends_system_and_user_messages() -> None:
    route = respx.post(GROQ_URL).mock(return_value=_groq_ok())
    provider = GroqProvider("m", ["key-1"])

    assert await provider.complete(REQUEST) == "hi there"

    body = json.loads(route.calls.last.request.content)
    assert body["messages"] == [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "hello"},
    ]
    assert body["model"] == "m"
    assert "response_format" not in body
    assert route.calls.last.request.headers["authorization"] == "Bearer key-1"


@respx.mock
async def test_groq_requests_json_mode_when_asked() -> None:
    route = respx.post(GROQ_URL).mock(return_value=_groq_ok('{"ok": true}'))
    await GroqProvider("m", ["k"]).complete(ChatRequest(system="s", user="u", json_object=True))

    body = json.loads(route.calls.last.request.content)
    assert body["response_format"] == {"type": "json_object"}


@respx.mock
async def test_groq_rotates_to_the_next_key_on_rate_limit() -> None:
    route = respx.post(GROQ_URL).mock(
        side_effect=[httpx.Response(429, text="rate limited"), _groq_ok("second key worked")]
    )
    provider = GroqProvider("m", ["spent-key", "fresh-key"])

    assert await provider.complete(REQUEST) == "second key worked"
    assert route.call_count == 2
    assert route.calls[0].request.headers["authorization"] == "Bearer spent-key"
    assert route.calls[1].request.headers["authorization"] == "Bearer fresh-key"


@respx.mock
async def test_groq_fails_once_every_key_is_exhausted() -> None:
    route = respx.post(GROQ_URL).mock(return_value=httpx.Response(429, text="quota"))
    with pytest.raises(LLMError, match="after trying 2 key"):
        await GroqProvider("m", ["a", "b"]).complete(REQUEST)
    assert route.call_count == 2


@respx.mock
async def test_groq_retries_a_server_error_on_the_same_key() -> None:
    route = respx.post(GROQ_URL).mock(
        side_effect=[httpx.Response(503, text="unavailable"), _groq_ok("recovered")]
    )
    provider = GroqProvider("m", ["only-key"], timeout=5)

    assert await provider.complete(REQUEST) == "recovered"
    assert route.call_count == 2


@respx.mock
async def test_groq_does_not_retry_a_client_error() -> None:
    route = respx.post(GROQ_URL).mock(return_value=httpx.Response(400, text="bad model"))
    with pytest.raises(LLMError, match="rejected the request"):
        await GroqProvider("m", ["k"]).complete(REQUEST)
    assert route.call_count == 1


@respx.mock
async def test_groq_rejects_an_empty_completion() -> None:
    respx.post(GROQ_URL).mock(return_value=_groq_ok("   "))
    with pytest.raises(LLMError, match="empty completion"):
        await GroqProvider("m", ["k"]).complete(REQUEST)


@respx.mock
async def test_groq_reports_an_unexpected_response_shape() -> None:
    respx.post(GROQ_URL).mock(return_value=httpx.Response(200, json={"unexpected": True}))
    with pytest.raises(LLMError, match="Unexpected Groq response"):
        await GroqProvider("m", ["k"]).complete(REQUEST)


# --- Gemini ------------------------------------------------------------------


@respx.mock
async def test_gemini_sends_system_instruction_and_key_header() -> None:
    route = respx.post(GEMINI_URL).mock(return_value=_gemini_ok())
    assert await GeminiProvider("gemini-test", ["key-1"]).complete(REQUEST) == "hi there"

    body = json.loads(route.calls.last.request.content)
    assert body["systemInstruction"]["parts"][0]["text"] == "be terse"
    assert body["contents"][0]["parts"][0]["text"] == "hello"
    assert "tools" not in body
    assert route.calls.last.request.headers["x-goog-api-key"] == "key-1"


@respx.mock
async def test_gemini_enables_search_grounding_when_configured() -> None:
    route = respx.post(GEMINI_URL).mock(return_value=_gemini_ok())
    await GeminiProvider("gemini-test", ["k"], enable_search=True).complete(REQUEST)

    body = json.loads(route.calls.last.request.content)
    assert body["tools"] == [{"google_search": {}}]


@respx.mock
async def test_gemini_joins_multi_part_answers() -> None:
    respx.post(GEMINI_URL).mock(
        return_value=httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"text": "one"}, {"text": "two"}]}}]},
        )
    )
    assert await GeminiProvider("gemini-test", ["k"]).complete(REQUEST) == "one\ntwo"


@respx.mock
async def test_gemini_rotates_keys_on_quota_exhaustion() -> None:
    route = respx.post(GEMINI_URL).mock(
        side_effect=[httpx.Response(429, text="RESOURCE_EXHAUSTED"), _gemini_ok("worked")]
    )
    assert await GeminiProvider("gemini-test", ["a", "b"]).complete(REQUEST) == "worked"
    assert route.call_count == 2


@respx.mock
async def test_gemini_explains_a_blocked_response() -> None:
    respx.post(GEMINI_URL).mock(
        return_value=httpx.Response(200, json={"candidates": [{"finishReason": "SAFETY"}]})
    )
    with pytest.raises(LLMError, match="finishReason=SAFETY"):
        await GeminiProvider("gemini-test", ["k"]).complete(REQUEST)


@respx.mock
async def test_transport_errors_are_retried_then_surfaced() -> None:
    route = respx.post(GROQ_URL).mock(side_effect=httpx.ConnectError("no route"))

    with pytest.raises(LLMError, match="transport error"):
        await GroqProvider("m", ["k"]).complete(REQUEST)
    assert route.call_count == 2


# --- Stub --------------------------------------------------------------------


async def test_stub_returns_valid_json_when_json_mode_is_requested() -> None:
    output = await StubProvider("x").complete(
        ChatRequest(system="s", user="Agent protocols", json_object=True)
    )
    assert set(json.loads(output)) >= {"themes", "risks", "open_questions"}


async def test_stub_output_tracks_the_prompt() -> None:
    output = await StubProvider("x").complete(ChatRequest(system="s", user="Quantum networking"))
    assert "Quantum networking" in output
