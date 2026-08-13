"""Agent cards — the identity and capability documents of the A2A protocol.

Each agent publishes its card at ``/.well-known/agent-card.json``. That document
is the *only* thing another agent needs in order to work with it: it names the
agent, the transport binding and URL to reach it on, and the skills it offers.
The coordinator routes purely on the skill ids it finds in discovered cards, so
nothing in this system imports another agent's implementation.
"""

from __future__ import annotations

from a2a.types import a2a_pb2
from a2a.utils import TransportProtocol
from a2a.utils.constants import PROTOCOL_VERSION_CURRENT

from research_desk import __version__
from research_desk.config import AgentName

PROVIDER = a2a_pb2.AgentProvider(
    organization="Research Desk",
    url="https://github.com/mallahim01/a2a",
)

#: Skill ids the coordinator resolves against discovered cards. These strings are
#: the contract between agents — renaming one breaks discovery, not an import.
SKILL_GATHER_SOURCES = "gather_sources"
SKILL_EXTRACT_INSIGHTS = "extract_insights"
SKILL_COMPOSE_BRIEF = "compose_brief"
SKILL_RESEARCH_BRIEF = "research_brief"

_TEXT = ["text/plain"]
_TEXT_AND_MARKDOWN = ["text/plain", "text/markdown"]


def _card(
    *,
    name: str,
    description: str,
    url: str,
    skills: list[a2a_pb2.AgentSkill],
    output_modes: list[str],
) -> a2a_pb2.AgentCard:
    return a2a_pb2.AgentCard(
        name=name,
        description=description,
        version=__version__,
        provider=PROVIDER,
        documentation_url="https://github.com/mallahim01/a2a#readme",
        supported_interfaces=[
            a2a_pb2.AgentInterface(
                url=url,
                protocol_binding=TransportProtocol.JSONRPC,
                protocol_version=PROTOCOL_VERSION_CURRENT,
            )
        ],
        capabilities=a2a_pb2.AgentCapabilities(streaming=True, push_notifications=False),
        default_input_modes=_TEXT,
        default_output_modes=output_modes,
        skills=skills,
    )


def coordinator_card(url: str) -> a2a_pb2.AgentCard:
    return _card(
        name="Coordinator",
        description=(
            "Entry point of the research desk. Discovers the specialist agents on its "
            "network, plans the work, delegates each step over A2A, and assembles the "
            "final brief from what the specialists return."
        ),
        url=url,
        output_modes=_TEXT_AND_MARKDOWN,
        skills=[
            a2a_pb2.AgentSkill(
                id=SKILL_RESEARCH_BRIEF,
                name="Research brief",
                description=(
                    "Turn an open research question into a written brief by orchestrating "
                    "the researcher, analyst and writer agents."
                ),
                tags=["orchestration", "research", "a2a"],
                examples=[
                    "Brief me on the state of open agent interoperability protocols",
                    "What should a platform team know about WebAssembly at the edge?",
                ],
                input_modes=_TEXT,
                output_modes=_TEXT_AND_MARKDOWN,
            )
        ],
    )


def researcher_card(url: str) -> a2a_pb2.AgentCard:
    return _card(
        name="Researcher",
        description=(
            "Gathers the factual landscape for a topic: what exists, who the players "
            "are, what changed recently, and where the disagreements lie."
        ),
        url=url,
        output_modes=_TEXT_AND_MARKDOWN,
        skills=[
            a2a_pb2.AgentSkill(
                id=SKILL_GATHER_SOURCES,
                name="Gather background",
                description=(
                    "Produce a structured factual overview of a topic, including notable "
                    "developments, competing approaches and known unknowns."
                ),
                tags=["research", "background", "landscape"],
                examples=["Open agent interoperability protocols"],
                input_modes=_TEXT,
                output_modes=_TEXT_AND_MARKDOWN,
            )
        ],
    )


def analyst_card(url: str) -> a2a_pb2.AgentCard:
    return _card(
        name="Analyst",
        description=(
            "Reads raw research and distils it into machine-readable findings: themes, "
            "risks, open questions and a confidence rating."
        ),
        url=url,
        output_modes=["text/plain", "application/json"],
        skills=[
            a2a_pb2.AgentSkill(
                id=SKILL_EXTRACT_INSIGHTS,
                name="Extract insights",
                description=(
                    "Convert unstructured research notes into a JSON object of themes, "
                    "risks, open questions and confidence."
                ),
                tags=["analysis", "structured-output", "json"],
                examples=["<research notes to distil>"],
                input_modes=_TEXT,
                output_modes=["application/json"],
            )
        ],
    )


def writer_card(url: str) -> a2a_pb2.AgentCard:
    return _card(
        name="Writer",
        description=(
            "Composes the final decision-ready brief in Markdown from the research "
            "notes and the analyst's structured findings."
        ),
        url=url,
        output_modes=_TEXT_AND_MARKDOWN,
        skills=[
            a2a_pb2.AgentSkill(
                id=SKILL_COMPOSE_BRIEF,
                name="Compose brief",
                description=(
                    "Write a concise Markdown brief with a summary, key findings, risks "
                    "and open questions."
                ),
                tags=["writing", "markdown", "synthesis"],
                examples=["<research notes plus structured findings>"],
                input_modes=_TEXT,
                output_modes=_TEXT_AND_MARKDOWN,
            )
        ],
    )


_BUILDERS = {
    AgentName.COORDINATOR: coordinator_card,
    AgentName.RESEARCHER: researcher_card,
    AgentName.ANALYST: analyst_card,
    AgentName.WRITER: writer_card,
}


def build_card(agent: AgentName, url: str) -> a2a_pb2.AgentCard:
    """Build the agent card for ``agent``, advertised at the given absolute URL."""
    return _BUILDERS[agent](url)
