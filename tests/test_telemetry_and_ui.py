"""Optional tracing, and the browser-side A2A console."""

from __future__ import annotations

import json
import re

import pytest

from research_desk import telemetry
from research_desk.config import AgentName, Settings
from research_desk.ui import UI_INDEX
from tests.a2a_helpers import RPC_HEADERS
from tests.conftest import Desk

# --- telemetry ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_tracing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset the process-global tracer between tests and never ship real spans.

    ``configure_tracing`` sets OpenTelemetry's global provider, which is
    write-once; without clearing it each test would silently reuse the first
    one. The exporter is stubbed so nothing dials a collector.
    """
    from opentelemetry import trace
    from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

    class NullExporter(SpanExporter):
        def __init__(self, **_: object) -> None: ...

        def export(self, spans: object) -> SpanExportResult:
            return SpanExportResult.SUCCESS

        def shutdown(self) -> None: ...

    monkeypatch.setattr(
        "opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter",
        NullExporter,
    )

    def clear() -> None:
        telemetry._configured = False
        trace._TRACER_PROVIDER = None
        trace._TRACER_PROVIDER_SET_ONCE._done = False

    clear()
    yield
    clear()


def test_tracing_is_off_by_default() -> None:
    assert Settings().telemetry_enabled is False


def test_configure_tracing_does_nothing_when_disabled() -> None:
    assert telemetry.configure_tracing("svc", enabled=False, endpoint="http://x:4318") is False


def test_configure_tracing_reports_active_and_installs_only_one_provider() -> None:
    """`research-desk dev` builds four apps in one process.

    Each build calls configure_tracing, so the second call must still report
    that tracing is on — otherwise only the first agent gets instrumented —
    while not stacking a second provider and exporter.
    """
    from opentelemetry import trace

    assert telemetry.configure_tracing("svc", enabled=True, endpoint="http://x:4318") is True
    first = trace.get_tracer_provider()

    assert telemetry.configure_tracing("svc", enabled=True, endpoint="http://x:4318") is True
    assert trace.get_tracer_provider() is first


def test_instrument_app_is_a_passthrough_when_disabled() -> None:
    sentinel = object()

    assert telemetry.instrument_app(sentinel, enabled=False) is sentinel  # type: ignore[arg-type]


def test_instrument_app_wraps_when_enabled() -> None:
    sentinel = object()

    assert telemetry.instrument_app(sentinel, enabled=True) is not sentinel  # type: ignore[arg-type]


def test_span_and_annotate_are_safe_no_ops_when_tracing_is_off() -> None:
    with telemetry.span("noop", peer="Writer"):
        telemetry.annotate_task("ctx", "task")


def test_span_records_attributes_when_tracing_is_on() -> None:
    from opentelemetry import trace

    telemetry.configure_tracing("svc", enabled=True, endpoint="http://x:4318")
    with telemetry.span("a2a.delegate test", peer="Writer", skill="compose_brief"):
        assert trace.get_current_span().is_recording()


def test_every_agent_in_a_shared_process_gets_instrumented(settings: Settings) -> None:
    """Guards the `dev`-mode regression: only the first agent was traced."""
    from research_desk.agents import build_agent_app

    traced = settings.model_copy(update={"telemetry_enabled": True})

    # An instrumented app is the OTel middleware, not a bare Starlette app.
    wrapped = [
        type(build_agent_app(agent, traced)).__name__
        for agent in (AgentName.RESEARCHER, AgentName.ANALYST, AgentName.WRITER)
    ]

    assert all(name == "OpenTelemetryMiddleware" for name in wrapped), wrapped


def test_apps_are_not_wrapped_when_tracing_is_off(settings: Settings) -> None:
    from research_desk.agents import build_agent_app

    assert type(build_agent_app(AgentName.WRITER, settings)).__name__ == "Starlette"


# --- the console -------------------------------------------------------------


async def test_the_console_is_served_by_the_coordinator(desk: Desk) -> None:
    response = await desk.client.get(f"{desk.url(AgentName.COORDINATOR)}/ui")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


async def test_only_the_coordinator_serves_the_console(desk: Desk) -> None:
    response = await desk.client.get(f"{desk.url(AgentName.WRITER)}/ui")

    assert response.status_code != 200


def test_the_console_is_self_contained() -> None:
    """No CDN scripts or external stylesheets — it must work on an air-gapped box."""
    html = UI_INDEX.read_text(encoding="utf-8")

    assert not re.search(r"<script[^>]+src=", html)
    assert not re.search(r"<link[^>]+stylesheet", html)


def test_the_console_speaks_the_protocol_correctly() -> None:
    """It is an A2A client, so it must send what a v1.0 server demands."""
    html = UI_INDEX.read_text(encoding="utf-8")

    assert '"A2A-Version": "1.0"' in html
    assert "SendStreamingMessage" in html
    assert "text/event-stream" in html
    assert "X-API-Key" in html
    # SSE frames are CRLF-separated; without normalising, nothing ever parses.
    assert r'replace(/\r\n/g, "\n")' in html


async def test_the_stream_the_console_parses_is_shaped_as_it_expects(desk: Desk) -> None:
    """The console parses raw SSE by hand, so its assumptions are pinned here.

    It reads ``data:`` frames separated by a blank line, and inside each frame a
    JSON-RPC envelope whose result is ``task`` / ``statusUpdate`` /
    ``artifactUpdate`` — camelCase, as proto JSON emits.
    """
    frames: list[dict] = []
    async with desk.client.stream(
        "POST",
        f"{desk.url(AgentName.COORDINATOR)}/",
        headers={**RPC_HEADERS, "Accept": "text/event-stream"},
        json={
            "jsonrpc": "2.0",
            "id": "ui-1",
            "method": "SendStreamingMessage",
            "params": {
                "message": {
                    "messageId": "ui-1",
                    "role": "ROLE_USER",
                    "parts": [{"text": "the state of open agent interoperability protocols"}],
                }
            },
        },
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")

        buffer = ""
        async for chunk in response.aiter_text():
            # Frames arrive CRLF-separated; the console normalises the same way.
            buffer += chunk.replace("\r\n", "\n")
            while "\n\n" in buffer:
                frame, buffer = buffer.split("\n\n", 1)
                payload = "".join(
                    line[5:].strip() for line in frame.split("\n") if line.startswith("data:")
                )
                if payload:
                    frames.append(json.loads(payload))

    results = [frame["result"] for frame in frames]
    assert frames, "no SSE frames parsed — check the frame separator"
    assert all(frame["jsonrpc"] == "2.0" for frame in frames)
    assert any("task" in r for r in results)

    updates = [r["statusUpdate"]["status"] for r in results if "statusUpdate" in r]
    assert any(u["state"] == "TASK_STATE_WORKING" for u in updates)
    assert any(u["state"] == "TASK_STATE_COMPLETED" for u in updates)

    # The console keys its animation off these exact phrases.
    texts = [
        part["text"]
        for u in updates
        for part in u.get("message", {}).get("parts", [])
        if "text" in part
    ]
    assert any(t.startswith("Delegating '") for t in texts)
    assert any("returned" in t for t in texts)

    artifacts = {r["artifactUpdate"]["artifact"]["name"] for r in results if "artifactUpdate" in r}
    assert artifacts == {"brief.md", "collaboration.json"}


def test_the_console_graph_matches_the_real_agents_and_skills() -> None:
    """A diagram that drifts from the system is worse than no diagram."""
    from research_desk.cards import build_card

    html = UI_INDEX.read_text(encoding="utf-8")
    for agent in AgentName:
        card = build_card(agent, "http://x.test/")
        assert card.name in html
        for skill in card.skills:
            assert skill.id in html
