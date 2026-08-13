"""Coordinator agent — the orchestrator, and the clearest view of A2A at work.

It is an A2A **server** to whoever asks it a question, and an A2A **client** to
the three specialists. It holds no research, analysis or writing logic of its
own and imports none of the specialist modules; everything it knows about its
collaborators arrives at runtime in their agent cards.

The pipeline:

    plan → gather_sources → extract_insights → compose_brief

Each arrow is a JSON-RPC ``SendMessage`` call to whichever discovered agent
advertises that skill. Alongside the finished brief the coordinator publishes a
``collaboration.json`` artifact recording every hop, so the protocol traffic is
visible in the response itself rather than only in the logs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from a2a.helpers import new_data_part, new_task_from_user_message, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import a2a_pb2

from research_desk.cards import (
    SKILL_COMPOSE_BRIEF,
    SKILL_EXTRACT_INSIGHTS,
    SKILL_GATHER_SOURCES,
)
from research_desk.llm import ChatRequest, LLMError, LLMProvider
from research_desk.logging import bind_task, get_logger
from research_desk.protocol.client import PeerClient, PeerError, PeerResult
from research_desk.protocol.discovery import AgentRegistry, SkillNotAvailableError

logger = get_logger(__name__)

MIN_QUERY_CHARS = 12
MIN_QUERY_WORDS = 3

PLANNER_SYSTEM_PROMPT = """\
You turn a research question into a short research directive for a colleague.

Return 2-4 lines of plain text: the topic restated precisely, then the specific \
angles worth investigating. No headings, no numbering, no commentary.
"""


@dataclass(slots=True)
class Collaboration:
    """Running record of one orchestrated request."""

    question: str
    hops: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def record(self, result: PeerResult) -> None:
        self.hops.append(result.as_trace())

    def note(self, message: str) -> None:
        self.notes.append(message)
        logger.warning("degraded", extra={"reason": message})

    def as_artifact(self, participants: list[str]) -> dict[str, Any]:
        return {
            "question": self.question,
            "participants": participants,
            "protocol": "A2A JSON-RPC (SendMessage)",
            "hops": self.hops,
            "degradations": self.notes,
        }


class CoordinatorExecutor(AgentExecutor):
    """Answers the ``research_brief`` skill by delegating over A2A."""

    def __init__(
        self,
        *,
        registry: AgentRegistry,
        peer_client: PeerClient,
        planner: LLMProvider,
    ) -> None:
        self._registry = registry
        self._peers = peer_client
        self._planner = planner

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task
        if task is None:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)

        bind_task(task.context_id, task.id)
        updater = TaskUpdater(event_queue, task.id, task.context_id)

        question = context.get_user_input().strip()
        if _too_vague(question):
            await updater.requires_input(
                updater.new_agent_message(
                    [
                        new_text_part(
                            "That question is too short to research. Send a topic of at "
                            "least a few words, for example: 'the state of open agent "
                            "interoperability protocols'."
                        )
                    ]
                )
            )
            return

        collaboration = Collaboration(question=question)
        await self._status(updater, "Discovering specialist agents")

        agents = await self._ensure_discovered()
        if not agents:
            await updater.failed(
                updater.new_agent_message(
                    [
                        new_text_part(
                            "No specialist agents could be discovered. Check that the "
                            "peers in PEER_AGENT_URLS are running and reachable."
                        )
                    ]
                )
            )
            return

        participants = [agent.name for agent in agents]
        await self._status(updater, f"Discovered {len(agents)} agent(s): {', '.join(participants)}")

        directive = await self._plan(question, updater, collaboration)

        try:
            research = await self._delegate(
                SKILL_GATHER_SOURCES,
                directive,
                updater=updater,
                collaboration=collaboration,
                context_id=task.context_id,
            )
        except (PeerError, SkillNotAvailableError) as exc:
            await updater.failed(
                updater.new_agent_message(
                    [new_text_part(f"Research stage failed, cannot continue: {exc}")]
                )
            )
            return

        findings = await self._try_delegate(
            SKILL_EXTRACT_INSIGHTS,
            research.text,
            updater=updater,
            collaboration=collaboration,
            context_id=task.context_id,
        )

        brief = await self._try_delegate(
            SKILL_COMPOSE_BRIEF,
            _writer_prompt(question, research.text, findings.text if findings else None),
            updater=updater,
            collaboration=collaboration,
            context_id=task.context_id,
        )

        document = (
            brief.text
            if brief
            else _fallback_brief(question, research.text, findings.text if findings else None)
        )

        await updater.add_artifact(
            [new_text_part(document)],
            name="brief.md",
            metadata={"produced_by": "coordinator", "question": question},
        )
        await updater.add_artifact(
            [new_data_part(collaboration.as_artifact(participants), media_type="application/json")],
            name="collaboration.json",
            metadata={"description": "A2A hops taken to produce this brief"},
        )
        await updater.complete()
        logger.info(
            "collaboration complete",
            extra={"hops": len(collaboration.hops), "degradations": len(collaboration.notes)},
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Cancellation is not supported: delegated peer tasks cannot be recalled."""
        updater = TaskUpdater(event_queue, context.task_id or "", context.context_id or "")
        await updater.reject(
            updater.new_agent_message(
                [new_text_part("Delegated work is already in flight and cannot be cancelled.")]
            )
        )

    async def _ensure_discovered(self) -> list[Any]:
        """Peers may still have been booting at startup, so retry discovery lazily."""
        if self._registry.agents:
            return self._registry.agents
        return await self._registry.discover()

    async def _plan(self, question: str, updater: TaskUpdater, collaboration: Collaboration) -> str:
        """Sharpen the question into a research directive.

        Purely an improvement: if the planner model is unavailable the raw
        question is forwarded unchanged and the run continues.
        """
        await self._status(updater, "Planning the research")
        try:
            directive = await self._planner.complete(
                ChatRequest(
                    system=PLANNER_SYSTEM_PROMPT,
                    user=question,
                    max_output_tokens=300,
                    temperature=0.2,
                )
            )
        except LLMError as exc:
            collaboration.note(f"planner unavailable ({exc}); forwarded the question verbatim")
            return question
        return directive.strip() or question

    async def _delegate(
        self,
        skill_id: str,
        prompt: str,
        *,
        updater: TaskUpdater,
        collaboration: Collaboration,
        context_id: str,
    ) -> PeerResult:
        agent = self._registry.by_skill(skill_id)
        await self._status(updater, f"Delegating '{skill_id}' to {agent.name}")
        result = await self._peers.delegate(
            card=agent.card,
            skill_id=skill_id,
            prompt=prompt,
            context_id=context_id,
        )
        collaboration.record(result)
        await self._status(updater, f"{agent.name} returned {len(result.text)} characters")
        return result

    async def _try_delegate(
        self,
        skill_id: str,
        prompt: str,
        *,
        updater: TaskUpdater,
        collaboration: Collaboration,
        context_id: str,
    ) -> PeerResult | None:
        """Delegate a stage the brief can survive without."""
        try:
            return await self._delegate(
                skill_id,
                prompt,
                updater=updater,
                collaboration=collaboration,
                context_id=context_id,
            )
        except (PeerError, SkillNotAvailableError) as exc:
            collaboration.note(f"skill '{skill_id}' unavailable: {exc}")
            await self._status(updater, f"Continuing without '{skill_id}'")
            return None

    @staticmethod
    async def _status(updater: TaskUpdater, message: str) -> None:
        """Publish progress so a streaming client can watch the collaboration."""
        logger.info(message)
        await updater.update_status(
            a2a_pb2.TASK_STATE_WORKING,
            message=updater.new_agent_message([new_text_part(message)]),
        )


def _too_vague(question: str) -> bool:
    """Guard that drives the ``input-required`` branch of the task lifecycle.

    Deliberately a simple heuristic rather than a model call — the point is to
    demonstrate the protocol state, not to judge question quality.
    """
    return len(question) < MIN_QUERY_CHARS or len(question.split()) < MIN_QUERY_WORDS


def _writer_prompt(question: str, research: str, findings: str | None) -> str:
    sections = [f"# Question\n{question}", f"# Research notes\n{research}"]
    if findings:
        sections.append(f"# Structured findings (JSON)\n{findings}")
    else:
        sections.append("# Structured findings\nUnavailable — the analyst agent did not respond.")
    return "\n\n".join(sections)


def _fallback_brief(question: str, research: str, findings: str | None) -> str:
    """Assemble a brief without the writer agent.

    Not a hidden re-implementation of the writer: it is a plain concatenation,
    clearly labelled, so a missing peer degrades the answer instead of losing it.
    """
    parts = [
        f"# {question}",
        "> Assembled by the coordinator: the writer agent was unavailable, so this "
        "brief is the raw material rather than an edited document.",
        "## Research notes",
        research,
    ]
    if findings:
        parts += ["## Structured findings", f"```json\n{_pretty(findings)}\n```"]
    return "\n\n".join(parts)


def _pretty(payload: str) -> str:
    try:
        return json.dumps(json.loads(payload), indent=2)
    except json.JSONDecodeError:
        return payload
