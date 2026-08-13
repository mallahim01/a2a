"""API-key authentication, declared in the agent card and enforced at the edge.

A2A does not invent an auth mechanism. An agent *declares* what it accepts in
its card's ``securitySchemes`` and ``security`` fields, and clients read that
declaration to work out what to send. This module implements both halves of one
scheme — API key in a header — the way the specification intends:

* :func:`api_key_security_scheme` puts the declaration in the card.
* :class:`ApiKeyAuthMiddleware` enforces it on the JSON-RPC endpoint.
* :class:`StaticApiKeyCredentials` feeds the SDK's ``AuthInterceptor``, which
  reads the *callee's* card and attaches the header that card asks for.

The agent card and ``/health`` stay public: discovery has to work before a
client can know what credentials to present.
"""

from __future__ import annotations

from a2a.client import ClientCallContext, CredentialService
from a2a.types import a2a_pb2
from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from research_desk.logging import get_logger

logger = get_logger(__name__)

#: Name of the scheme as it appears in the agent card.
API_KEY_SCHEME = "api_key"
#: Header the scheme asks callers to send.
API_KEY_HEADER = "X-API-Key"

#: Paths that stay reachable without credentials.
PUBLIC_PATHS = frozenset({AGENT_CARD_WELL_KNOWN_PATH, "/health", "/agents", "/ui"})


def api_key_security_scheme() -> a2a_pb2.SecurityScheme:
    """The card declaration: an API key, sent in a header."""
    return a2a_pb2.SecurityScheme(
        api_key_security_scheme=a2a_pb2.APIKeySecurityScheme(
            name=API_KEY_HEADER,
            location="header",
            description="Shared API key issued to agents and clients of this desk.",
        )
    )


def api_key_requirement() -> a2a_pb2.SecurityRequirement:
    """The card's statement that the scheme above is required."""
    requirement = a2a_pb2.SecurityRequirement()
    requirement.schemes[API_KEY_SCHEME].CopyFrom(a2a_pb2.StringList())
    return requirement


class StaticApiKeyCredentials(CredentialService):
    """Supplies one API key for the ``api_key`` scheme.

    Handed to the SDK's ``AuthInterceptor``, which decides *how* to send it by
    reading the callee's agent card — so a peer that later switches to bearer
    tokens is accommodated without changing the caller.
    """

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    async def get_credentials(
        self, security_scheme_name: str, context: ClientCallContext | None = None
    ) -> str | None:
        return self._api_key if security_scheme_name == API_KEY_SCHEME else None


class ApiKeyAuthMiddleware:
    """Rejects unauthenticated calls to the protocol endpoint.

    Pure ASGI rather than ``BaseHTTPMiddleware`` so that streaming responses on
    the JSON-RPC route are not buffered.
    """

    def __init__(self, app: ASGIApp, api_key: str) -> None:
        self._app = app
        self._api_key = api_key

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self._is_public(scope):
            await self._app(scope, receive, send)
            return

        if self._presented_key(scope) == self._api_key:
            await self._app(scope, receive, send)
            return

        logger.warning(
            "rejected unauthenticated request",
            extra={"path": scope.get("path", ""), "method": scope.get("method", "")},
        )
        response = JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": None,
                "error": {
                    "code": -32001,
                    "message": (
                        f"Missing or invalid API key. Send it in the {API_KEY_HEADER} "
                        f"header; see securitySchemes in {AGENT_CARD_WELL_KNOWN_PATH}."
                    ),
                },
            },
            status_code=401,
            headers={"WWW-Authenticate": API_KEY_HEADER},
        )
        await response(scope, receive, send)

    @staticmethod
    def _is_public(scope: Scope) -> bool:
        path = scope.get("path", "")
        return path in PUBLIC_PATHS or path.startswith("/ui")

    @staticmethod
    def _presented_key(scope: Scope) -> str:
        wanted = API_KEY_HEADER.lower().encode()
        for name, value in scope.get("headers", []):
            if name.lower() == wanted:
                return value.decode()
        return ""
