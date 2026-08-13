"""Shared executor behaviour for the three specialist agents.

An ``AgentExecutor`` is the bridge between the protocol and the work. Every
specialist here follows the same lifecycle, so it is written once:

1. Register the task, if the request did not resume an existing one.
2. Move it to ``TASK_STATE_WORKING``.
3. Call the configured model.
4. Publish the answer as an artifact and complete the task — or, on failure,
   move to ``TASK_STATE_FAILED`` with an explanation the caller can read.

Step 4 matters for A2A: results travel as *artifacts* attached to a task, not as
return values, which is what lets a caller fetch them later with ``GetTask``.
"""

from __future__ import annotations

import json
from typing import Any

from a2a.helpers import new_data_part, new_task_from_user_message, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import a2a_pb2

from research_desk.llm import ChatRequest, LLMError, LLMProvider
from research_desk.logging import bind_task, get_logger
from research_desk.telemetry import span

logger = get_logger(__name__)

MIN_PROMPT_CHARS = 8


class LLMAgentExecutor(AgentExecutor):
    """Base for single-skill agents that answer with one model call."""

    #: Name of the artifact this agent publishes.
    artifact_name: str = "result.md"
    #: System prompt handed to the model.
    system_prompt: str = ""
    #: Request a single JSON object from the model.
    json_output: bool = False

    def __init__(
        self,
        provider: LLMProvider,
        *,
        agent_label: str,
        max_output_tokens: int = 2048,
        temperature: float = 0.3,
    ) -> None:
        self._provider = provider
        self._agent_label = agent_label
        self._max_output_tokens = max_output_tokens
        self._temperature = temperature

    def build_user_prompt(self, user_input: str) -> str:
        """Turn the incoming A2A message text into the model's user prompt."""
        return user_input

    def artifact_metadata(self, output: str) -> dict[str, Any]:
        """Metadata attached to the published artifact."""
        return {"produced_by": self._agent_label, "model": self._provider.name}

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task
        if task is None:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)

        bind_task(task.context_id, task.id)
        # An explicit span per agent, so a trace reads as "who did what" rather
        # than as the SDK's internal queue machinery.
        with span(
            f"a2a.agent {self._agent_label}",
            agent=self._agent_label,
            context_id=task.context_id,
            task_id=task.id,
        ):
            await self._run(context, event_queue, task)

    async def _run(
        self, context: RequestContext, event_queue: EventQueue, task: a2a_pb2.Task
    ) -> None:
        updater = TaskUpdater(event_queue, task.id, task.context_id)

        user_input = context.get_user_input().strip()
        if len(user_input) < MIN_PROMPT_CHARS:
            await updater.requires_input(
                updater.new_agent_message([new_text_part("Send a topic or some notes to work on.")])
            )
            return

        await updater.start_work(
            updater.new_agent_message(
                [new_text_part(f"{self._agent_label} working via {self._provider.name}")]
            )
        )
        logger.info(
            "agent working",
            extra={"model": self._provider.name, "input_chars": len(user_input)},
        )

        try:
            output = await self._provider.complete(
                ChatRequest(
                    system=self.system_prompt,
                    user=self.build_user_prompt(user_input),
                    max_output_tokens=self._max_output_tokens,
                    temperature=self._temperature,
                    json_object=self.json_output,
                )
            )
        except LLMError as exc:
            logger.error("model call failed", extra={"error": str(exc)})
            await updater.failed(updater.new_agent_message([new_text_part(str(exc))]))
            return

        await updater.add_artifact(
            self._build_parts(output),
            name=self.artifact_name,
            metadata=self.artifact_metadata(output),
        )
        await updater.complete()
        logger.info("agent completed", extra={"output_chars": len(output)})

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Cancellation is not supported: a single model call is not interruptible."""
        task_id = context.task_id or ""
        context_id = context.context_id or ""
        updater = TaskUpdater(event_queue, task_id, context_id)
        await updater.reject(
            updater.new_agent_message(
                [new_text_part("This agent runs one uninterruptible model call and cannot cancel.")]
            )
        )

    def _build_parts(self, output: str) -> list[a2a_pb2.Part]:
        """Wrap the model output in A2A parts.

        JSON answers are published twice — once as text for humans reading the
        artifact, once as a structured ``DataPart`` so a calling agent can consume
        the fields without re-parsing prose.
        """
        if not self.json_output:
            return [new_text_part(output)]
        try:
            parsed = json.loads(output)
        except json.JSONDecodeError:
            logger.warning("model promised JSON but returned prose; publishing as text")
            return [new_text_part(output)]
        return [
            new_text_part(json.dumps(parsed, indent=2)),
            new_data_part(parsed, media_type="application/json"),
        ]
