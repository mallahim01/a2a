"""The four agents, and the factory that turns one into a running server."""

from research_desk.agents.analyst import AnalystExecutor
from research_desk.agents.base import LLMAgentExecutor
from research_desk.agents.coordinator import CoordinatorExecutor
from research_desk.agents.factory import build_agent_app
from research_desk.agents.researcher import ResearcherExecutor
from research_desk.agents.writer import WriterExecutor

__all__ = [
    "AnalystExecutor",
    "CoordinatorExecutor",
    "LLMAgentExecutor",
    "ResearcherExecutor",
    "WriterExecutor",
    "build_agent_app",
]
