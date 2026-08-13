"""The A2A protocol layer: serving, discovering and calling agents.

Nothing in here knows anything about research, analysis or writing — it is the
transport and discovery machinery the four agents in
:mod:`research_desk.agents` are built on.
"""

from research_desk.protocol.client import PeerClient, PeerError, PeerResult
from research_desk.protocol.discovery import (
    AgentRegistry,
    DiscoveredAgent,
    SkillNotAvailableError,
)
from research_desk.protocol.server import build_app

__all__ = [
    "AgentRegistry",
    "DiscoveredAgent",
    "PeerClient",
    "PeerError",
    "PeerResult",
    "SkillNotAvailableError",
    "build_app",
]
