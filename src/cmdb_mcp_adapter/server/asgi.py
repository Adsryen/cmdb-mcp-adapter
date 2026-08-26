"""ASGI application for the Streamable HTTP transport.

This module turns the shared MCP server object (the one
:func:`cmdb_mcp_adapter.server.app.create_app` builds) into a single
Starlette ASGI application with three routes:

* ``POST/GET/DELETE /mcp`` — the MCP Streamable HTTP protocol, bridged to
  the low-level ``mcp.server.Server`` via the SDK's
  :class:`~mcp.server.streamable_http_manager.StreamableHTTPSessionManager`.
* ``GET /healthz`` — liveness probe (always ``200 {"status": "ok"}`` once the
  process is running).
* ``GET /readyz`` — readiness probe (``200`` when the configuration is valid
  and the upstream probe passes; otherwise ``503`` with a redacted
  ``reasons`` list).

Security
--------

* A **non-loopback bind** requires a ``Bearer`` token on every ``/mcp``
  request; a missing or wrong token yields ``401`` with a body that never
  contains the configured token.  The decision is delegated to
  :func:`cmdb_mcp_adapter.server.auth.make_auth_middleware` so the auth
  policy has exactly one owner.  ``/healthz`` and ``/readyz`` are
  *not* authenticated (internal probes).
* Any readiness ``reasons`` text is run through the shared redaction filter
  before it is returned, so a malformed configuration can never leak a
  secret into the response body.

The ASGI app owns no vendor logic and no tool implementation: it is a thin
transport boundary around the shared MCP server, mirroring the contract
that the stdio launcher uses for the same object.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from mcp.server.lowlevel.server import Server as LowLevelServer
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from cmdb_mcp_adapter.config import Settings
from cmdb_mcp_adapter.observability.redaction import redact
from cmdb_mcp_adapter.server import health as _health
from cmdb_mcp_adapter.server.auth import make_auth_middleware
from cmdb_mcp_adapter.server.http import HEALTH_ENDPOINT, MCP_ENDPOINT, READY_ENDPOINT


def _extract_low_level_server(mcp_server: Any) -> LowLevelServer:
    """Return the low-level ``mcp.server.Server`` behind a FastMCP instance.

    ``create_app`` returns a :class:`~mcp.server.fastmcp.FastMCP` whose
    ``_mcp_server`` attribute has been replaced by the low-level server that
    :func:`cmdb_mcp_adapter.server.mcp_server.build_mcp_server` built (with
    the seven registered tool handlers).  The Streamable HTTP session
    manager drives that low-level server directly.

    A bare low-level server is accepted unchanged so callers that already
    have the inner object (tests) can pass it without a wrapper.
    """
    candidate = getattr(mcp_server, "_mcp_server", None)
    if candidate is None:
        candidate = mcp_server
    return candidate  # type: ignore[no-any-return]


def _headers_to_dict(scope: Scope) -> dict[str, Any]:
    """Convert an ASGI ``scope["headers"]`` (list of byte tuples) into a
    string-keyed mapping suitable for :func:`make_auth_middleware`.
    """
    headers: dict[str, Any] = {}
    for name, value in scope.get("headers", []):
        # ASGI header names are lower-cased bytes.
        key = name.decode("latin-1")
        headers[key] = value.decode("latin-1")
    return headers


class AuthMiddleware:
    """ASGI middleware that enforces the inbound bearer-token policy.

    Only requests to :data:`MCP_ENDPOINT` are authenticated; the
    liveness/readiness probes are intentionally left open for internal
    load-balancer / orchestrator probes.  The actual decision is delegated
    to the shared :func:`make_auth_middleware` callable so the auth policy
    has one owner.  A rejection produces a ``401`` whose body is
    ``{"status": "unauthorized", "reason": <generic>}``; the generic reason
    comes from :func:`authorize` and never names the configured token (we
    additionally run it through redaction for belt-and-braces).
    """

    def __init__(
        self,
        app: ASGIApp,
        auth_check: Callable[[dict[str, Any]], str | None],
        secrets: list[str] | None = None,
    ) -> None:
        self._app = app
        self._auth_check = auth_check
        self._secrets = secrets or []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path != MCP_ENDPOINT:
            await self._app(scope, receive, send)
            return
        error = self._auth_check(_headers_to_dict(scope))
        if error is None:
            await self._app(scope, receive, send)
            return
        # The reason text from authorize() is already generic (it never
        # includes the configured token).  Redact it anyway so a future
        # change to authorize() cannot leak a secret; we pass the real
        # secret list from the settings the middleware was built with.
        redacted_reason = redact(error, self._secrets) if isinstance(error, str) else error
        response = JSONResponse(
            content={"status": "unauthorized", "reason": redacted_reason},
            status_code=401,
        )
        await response(scope, receive, send)


def _make_health_endpoint() -> Callable[[Request], Awaitable[JSONResponse]]:
    async def health_endpoint(_request: Request) -> JSONResponse:
        body = _health.check_liveness()
        return JSONResponse(content=body, status_code=200)

    return health_endpoint


def _upstream_secrets(upstream: Any | None) -> list[str]:
    """Best-effort extraction of upstream CMDB credential values (known
    secrets for :func:`redact`) from whatever ``upstream`` object is
    supplied.

    The readiness endpoint accepts any object here; the production path
    passes an :class:`~cmdb_mcp_adapter.config.UpstreamSettings`, whose
    :meth:`redaction_secrets` covers the key/secret pair and the legacy
    0.1.0 token.  A missing/odd shape yields an empty list rather than
    raising — a probe bug must never take the readiness endpoint down.
    """
    secrets = getattr(upstream, "redaction_secrets", None)
    if callable(secrets):
        try:
            return [s for s in secrets() if isinstance(s, str) and s]
        except Exception:  # noqa: BLE001 - readiness must never raise
            return []
    return []




def _make_ready_endpoint(
    settings: Settings,
    prober: _health.UpstreamProber | None,
    upstream: Any | None,
) -> Callable[[Request], Awaitable[JSONResponse]]:
    async def ready_endpoint(_request: Request) -> JSONResponse:
        result = _health.check_readiness(settings, upstream=upstream, prober=prober)
        status = 200 if result.get("status") == "ok" else 503
        body: dict[str, Any]
        if status == 200:
            body = {"status": "ok"}
        else:
            body = {
                "status": "unavailable",
                "reasons": [str(result.get("reason", "unavailable"))],
            }
        # Redact the body before returning so a reason string that
        # happens to contain a credential (from a malformed config or
        # the upstream probe's own diagnostics) is scrubbed.  Both the
        # inbound MCP token and the *upstream* CMDB credentials (key /
        # secret / legacy token) are known secrets here, so none can
        # survive into the response.
        secrets: list[str] = [
            s
            for s in [settings.auth_token, *_upstream_secrets(upstream)]
            if s
        ]
        redacted_body = redact(body, secrets)
        return JSONResponse(content=redacted_body, status_code=status)

    return ready_endpoint


class _NotCatchAllMiddleware:
    """Innermost middleware: any HTTP path not matched by a route becomes
    a JSON 404.

    Starlette raises an internal ``HTTPException(404)`` when no route
    matches; this middleware intercepts that at the ASGI level by checking
    the path against the known endpoints *before* dispatching to the app,
    and short-circuits with a JSON 404 for anything else.  The check is
    intentionally small (three known paths) so it cannot drift from the
    route table.
    """

    _KNOWN: frozenset[str] = frozenset({MCP_ENDPOINT, HEALTH_ENDPOINT, READY_ENDPOINT})

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            path = scope.get("path", "")
            if path not in self._KNOWN:
                response = JSONResponse(
                    content={"status": "not_found", "path": path},
                    status_code=404,
                )
                await response(scope, receive, send)
                return
        await self._app(scope, receive, send)


class _MCPASGIApp:
    """Raw ASGI wrapper around the SDK session manager's ``handle_request``.

    Starlette's :class:`~starlette.routing.Route` treats a *class*
    ``endpoint`` as a raw ASGI application (it calls
    ``endpoint(scope, receive, send)`` directly) rather than wrapping it as
    an ``endpoint(request) -> response`` function.  This lets us mount the
    SDK's raw ASGI ``handle_request`` (which takes ``scope, receive, send``)
    at :data:`MCP_ENDPOINT` with the full method set (``GET`` / ``POST`` /
    ``DELETE``) and without a trailing-slash redirect (a
    :class:`~starlette.routing.Mount` would redirect ``/mcp`` to
    ``/mcp/``).
    """

    def __init__(self, session_manager: StreamableHTTPSessionManager) -> None:
        self._session_manager = session_manager

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._session_manager.handle_request(scope, receive, send)


def _make_lifespan(
    session_manager: StreamableHTTPSessionManager,
) -> Callable[[Any], Any]:
    """Build a Starlette lifespan callable that drives the session
    manager's :meth:`StreamableHTTPSessionManager.run` context.

    The SDK's session manager requires its ``run()`` context manager to be
    active before it will handle any request (``run()`` creates the
    internal task group that spawns per-session server tasks).  This
    lifespan is the single place where that context is entered and
    exited.  The SDK's own ``FastMCP.run_streamable_http_async`` does the
    same thing internally; we replicate it here so we can also add the
    health/ready routes and the auth middleware to the same Starlette app.

    Starlette's ``Lifespan`` type is ``Callable[[AppType],
    AbstractAsyncContextManager]``; we return a closure that, given the
    app, yields an async context manager that enters/exits
    ``session_manager.run()``.
    """

    @contextlib.asynccontextmanager
    async def _lifespan(_app: Any) -> AsyncIterator[None]:
        async with session_manager.run():
            yield

    return _lifespan


def build_asgi_app(
    settings: Settings,
    mcp_server: Any,
    *,
    prober: _health.UpstreamProber | None = None,
    upstream: Any | None = None,
) -> Starlette:
    """Build the full ASGI application for the HTTP transport.

    The ``prober`` / ``upstream`` arguments are the injectable readiness
    probe and upstream configuration (see
    :func:`cmdb_mcp_adapter.server.health.check_readiness`).  The MVP
    default prober is a no-op; the veops task supplies a real one.

    The returned app has, in dispatch order (outermost first):

    1. :class:`AuthMiddleware` — enforces the bearer-token policy on
       ``/mcp`` only (delegated to :func:`make_auth_middleware`);
    2. :class:`_NotCatchAllMiddleware` — turns any unknown path into a
       JSON 404 (runs before routing so it can short-circuit);
    3. the ``/mcp`` route (the SDK session manager's ``handle_request``,
       with the SDK's CORS/origin checks disabled);
    4. the ``/healthz`` route (liveness);
    5. the ``/readyz`` route (readiness; ``reasons`` are redacted).

    The session manager's ``run()`` context is driven by the Starlette
    lifespan hook (:func:`_make_lifespan`): a process running this app
    through uvicorn (or any ASGI server that triggers the lifespan
    protocol) needs no other setup.
    """
    low_level = _extract_low_level_server(mcp_server)

    # The SDK enables CORS (Access-Control-Allow-Origin: *) and an
    # Origin/Host allow-list on /mcp by default.  Both are inappropriate
    # here: the only clients of this endpoint are the MCP protocol
    # (token-authenticated, programmatic) — never browsers — so we turn
    # the CORS/origin checks off and keep Bearer auth as the single
    # inbound security boundary.
    session_manager = StreamableHTTPSessionManager(
        app=low_level,
        json_response=False,
        stateless=False,
        security_settings=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        ),
    )

    routes: list[Route] = [
        Route(
            MCP_ENDPOINT,
            endpoint=_MCPASGIApp(session_manager),
            methods=["GET", "POST", "DELETE"],
        ),
        Route(
            HEALTH_ENDPOINT,
            endpoint=_make_health_endpoint(),
            methods=["GET"],
        ),
        Route(
            READY_ENDPOINT,
            endpoint=_make_ready_endpoint(settings, prober, upstream),
            methods=["GET"],
        ),
    ]

    app = Starlette(routes=routes, lifespan=_make_lifespan(session_manager))
    # Innermost first: the catch-all 404 must run *before* routing so it
    # can short-circuit unknown paths without reaching the Router.
    app.add_middleware(_NotCatchAllMiddleware)
    # Outermost last: auth runs before the catch-all so an unknown path on
    # a non-loopback bind still yields 404 (not 401).
    secrets = [settings.auth_token] if settings.auth_token else []
    app.add_middleware(
        AuthMiddleware, auth_check=make_auth_middleware(settings), secrets=secrets
    )
    return app


__all__ = [
    "AuthMiddleware",
    "build_asgi_app",
    "HEALTH_ENDPOINT",
    "MCP_ENDPOINT",
    "READY_ENDPOINT",
]
