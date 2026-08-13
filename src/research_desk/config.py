"""Environment-driven configuration.

Every knob in the system is an environment variable so that the identical image
can be started as any of the four agents. Nothing here reads a hardcoded secret.
"""

from __future__ import annotations

import os
from enum import StrEnum
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentName(StrEnum):
    """The four agents that make up the demo."""

    COORDINATOR = "coordinator"
    RESEARCHER = "researcher"
    ANALYST = "analyst"
    WRITER = "writer"


DEFAULT_PORTS: dict[AgentName, int] = {
    AgentName.COORDINATOR: 8000,
    AgentName.RESEARCHER: 8001,
    AgentName.ANALYST: 8002,
    AgentName.WRITER: 8003,
}


def collect_api_keys(prefix: str, environ: dict[str, str] | None = None) -> list[str]:
    """Collect ``PREFIX``, ``PREFIX_2``, ``PREFIX_3``… into an ordered key pool.

    Providers use the pool for failover: when one key is rate-limited the next
    one is tried. Numbering stops at the first gap, and blanks are dropped.
    """
    env = os.environ if environ is None else environ
    keys: list[str] = []

    first = (env.get(prefix) or "").strip()
    if first:
        keys.append(first)

    index = 2
    while (raw := env.get(f"{prefix}_{index}")) is not None:
        if candidate := raw.strip():
            keys.append(candidate)
        index += 1

    return keys


class Settings(BaseSettings):
    """Runtime configuration, resolved from the process environment and ``.env``."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Model routing: "<provider>:<model>", provider in {groq, gemini, stub} ---
    coordinator_model: str = "groq:llama-3.1-8b-instant"
    researcher_model: str = "gemini:gemini-3.5-flash"
    analyst_model: str = "groq:openai/gpt-oss-120b"
    writer_model: str = "groq:llama-3.3-70b-versatile"

    researcher_enable_search: bool = False
    llm_timeout_seconds: float = 90.0
    llm_max_output_tokens: int = 2048
    llm_temperature: float = 0.3

    # --- Server ---
    host: str = "0.0.0.0"  # noqa: S104 - containers must bind all interfaces
    port: int | None = None
    public_url: str | None = None

    # --- Peer discovery ---
    peer_agent_urls: list[str] = Field(
        default_factory=lambda: [
            "http://127.0.0.1:8001",
            "http://127.0.0.1:8002",
            "http://127.0.0.1:8003",
        ]
    )
    discovery_retries: int = 10
    discovery_retry_delay_seconds: float = 1.5
    peer_request_timeout_seconds: float = 180.0

    # --- Logging ---
    log_level: str = "INFO"
    log_format: str = "console"

    @field_validator("peer_agent_urls", mode="before")
    @classmethod
    def _split_peer_urls(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip().rstrip("/") for item in value.split(",") if item.strip()]
        return value

    @property
    def groq_api_keys(self) -> list[str]:
        return collect_api_keys("GROQ_API_KEY")

    @property
    def google_api_keys(self) -> list[str]:
        return collect_api_keys("GOOGLE_API_KEY")

    def model_for(self, agent: AgentName) -> str:
        """The ``provider:model`` string configured for one agent."""
        return {
            AgentName.COORDINATOR: self.coordinator_model,
            AgentName.RESEARCHER: self.researcher_model,
            AgentName.ANALYST: self.analyst_model,
            AgentName.WRITER: self.writer_model,
        }[agent]

    def port_for(self, agent: AgentName) -> int:
        """The listen port: explicit ``PORT`` wins, otherwise the agent's default."""
        return self.port or DEFAULT_PORTS[agent]

    def public_url_for(self, agent: AgentName) -> str:
        """The absolute URL peers use to reach this agent.

        Advertised in the agent card, so it must be routable from other agents —
        under Docker that means the service name, not ``0.0.0.0``.
        """
        if self.public_url:
            return self.public_url if self.public_url.endswith("/") else f"{self.public_url}/"
        host = "127.0.0.1" if self.host in {"0.0.0.0", "::"} else self.host  # noqa: S104
        return f"http://{host}:{self.port_for(agent)}/"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()
