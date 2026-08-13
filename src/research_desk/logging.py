"""Structured logging.

Every log line carries the agent name and, where known, the A2A ``context_id``
and ``task_id``. Because the coordinator propagates its context id to each peer,
grepping one id across all four services reconstructs a whole collaboration.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from typing import Any

_agent_name: ContextVar[str] = ContextVar("agent_name", default="-")
_context_id: ContextVar[str] = ContextVar("context_id", default="")
_task_id: ContextVar[str] = ContextVar("task_id", default="")

_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "taskName",
    # uvicorn attaches an ANSI-coloured duplicate of its own message.
    "color_message",
}


def bind_agent(name: str) -> None:
    """Tag every subsequent log line in this process with the agent's name."""
    _agent_name.set(name)


def bind_task(context_id: str, task_id: str) -> None:
    """Tag log lines in the current async context with the A2A identifiers."""
    _context_id.set(context_id)
    _task_id.set(task_id)


def _extras(record: logging.LogRecord) -> dict[str, Any]:
    return {k: v for k, v in record.__dict__.items() if k not in _RESERVED}


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.agent = _agent_name.get()
        if context_id := _context_id.get():
            record.context_id = context_id
        if task_id := _task_id.get():
            record.task_id = task_id
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line — the format to use when shipping logs."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            **_extras(record),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class ConsoleFormatter(logging.Formatter):
    """Human-readable format for watching a demo run in a terminal."""

    def format(self, record: logging.LogRecord) -> str:
        extras = _extras(record)
        agent = extras.pop("agent", "-")
        context_id = extras.pop("context_id", "")
        suffix = " ".join(f"{k}={v}" for k, v in extras.items() if k != "task_id")
        head = f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<7} [{agent}]"
        if context_id:
            head += f" ctx={context_id[:8]}"
        line = f"{head} {record.getMessage()}"
        if suffix:
            line += f"  {suffix}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def configure_logging(level: str = "INFO", fmt: str = "console") -> None:
    """Install the root handler. Safe to call more than once."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt == "json" else ConsoleFormatter())
    handler.addFilter(_ContextFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # uvicorn installs its own handlers; route them through ours instead.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
