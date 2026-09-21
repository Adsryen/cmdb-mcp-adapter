"""Streamable HTTP transport launcher (long-running remote process).

The launcher is a thin boundary: it composes the shared MCP server through
:func:`cmdb_mcp_adapter.server.app.create_app`, builds the ASGI app through
:func:`cmdb_mcp_adapter.server.asgi.build_asgi_app`, and runs it with a
single uvicorn process.  It owns only the lifecycle:

* **Startup** — fail-closed configuration validation (inherited from
  :func:`create_app`), structured logging to ``stderr``, signal handlers
  for ``SIGINT`` / ``SIGTERM``.
* **Shutdown** — uvicorn's own signal handling is disabled so our
  :func:`cmdb_mcp_adapter.server.lifecycle.install_signal_handlers` event
  is the *only* shutdown trigger; when the event fires we cancel uvicorn's
  server and flush ``stderr`` so no structured log line is lost.
* **IO** — all diagnostics go to ``stderr``; ``stdout`` is reserved for the
  MCP protocol (the stdio transport writes frames there; the HTTP
  transport writes none because the MCP protocol rides on HTTP, not
  stdout).

The endpoint constants below are the shared contract for the remote-http
deployment task; they must match the routes registered by
:func:`cmdb_mcp_adapter.server.asgi.build_asgi_app`.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from typing import Any

import uvicorn

from cmdb_mcp_adapter.config import Settings
from cmdb_mcp_adapter.core import ConfigurationError, CoreError
from cmdb_mcp_adapter.observability.logging import configure_logging
from cmdb_mcp_adapter.server.app import create_app
from cmdb_mcp_adapter.server.lifecycle import (
    flush_stderr,
    install_signal_handlers,
    restore_default_signals,
    shutdown_event,
)

MCP_ENDPOINT = "/mcp"
HEALTH_ENDPOINT = "/healthz"
READY_ENDPOINT = "/readyz"

#: Default uvicorn access-log level.  We keep it at "info" so request
#: lines reach the structured stderr logger; a future task can lower it
#: to "warning" if traffic volume makes the noise unbearable.
_UVICORN_LOG_LEVEL = "info"


def _build_uvicorn_config(
    settings: Settings,
    app: Callable[..., Any],
    shutdown_event: asyncio.Event | None = None,
) -> uvicorn.Config:
    """Build a uvicorn Config for the ASGI app.

    ``log_config=None`` disables uvicorn's default logging config so our
    structured ``stderr`` JSON logger (from
    :func:`cmdb_mcp_adapter.observability.logging.configure_logging`) is
    the *only* log sink.  ``access_log`` is kept ``True`` so each request
#: line reaches the structured logger (the uvicorn ``uvicorn.access``
#: logger is a child of root and inherits our handler).

    ``lifespan="auto"`` triggers the ASGI app's lifespan hook, which
    drives the SDK's :class:`StreamableHTTPSessionManager.run` context
    (the task group that spawns per-session server tasks).  Without this,
    the session manager's task group is never initialized and every
    ``/mcp`` request fails with "Task group is not initialized".
    """
    return uvicorn.Config(
        app,
        host=settings.bind_host,
        port=settings.port,
        log_level=_UVICORN_LOG_LEVEL,
        log_config=None,
        access_log=True,
        lifespan="auto",
    )


def _serve_blocking(settings: Settings, app: Callable[..., Any]) -> None:
    """Run uvicorn in a fresh event loop and wait for it to finish.

    We construct our own :class:`uvicorn.Server` (rather than calling
    ``uvicorn.run``) so we can (a) run it in the same event loop as the
    rest of the process and (b) cancel it cleanly when the shared
    shutdown event fires.

    The shutdown watcher polls the shared :data:`shutdown_event` (a
    ``threading.Event`` set by our signal handler) and, when it fires,
    sets ``server.should_exit = True`` so uvicorn's ``serve()`` returns
    cleanly.
    """

    async def _inner() -> None:
        config = _build_uvicorn_config(settings, app)
        server = uvicorn.Server(config)
        loop = asyncio.get_running_loop()

        async def _watch_shutdown() -> None:
            while not shutdown_event.is_set():
                await asyncio.sleep(0.2)
            server.should_exit = True

        watch_task = loop.create_task(_watch_shutdown())
        try:
            await server.serve()
        finally:
            watch_task.cancel()

    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_inner())
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def run_http(settings: Settings) -> int:
    """Run the shared MCP server over Streamable HTTP.

    The entry point is synchronous (the CLI calls it from ``main``); it
    builds the app and then hands off to a fresh ``asyncio.run`` so the
    uvicorn server and the shutdown watcher share one event loop.

    Startup behavior:

    * structured logging to ``stderr`` is configured from
      ``settings.log_level`` (with the auth token registered as a
      redaction secret);
    * :func:`create_app` performs the fail-closed inbound validation
      (non-loopback bind requires ``MCP_AUTH_TOKEN``) and raises
      :class:`ConfigurationError` before any protocol activity;
    * signal handlers are installed so ``SIGINT`` / ``SIGTERM`` set the
      shared shutdown event (uvicorn's own handlers are disabled by
      ``uvicorn.Server`` running in the same loop — see
      :func:`_serve`);
    * on exit ``flush_stderr`` ensures no trailing structured log line is
      lost.
    """
    from cmdb_mcp_adapter.config import load_upstream_settings

    upstream = load_upstream_settings()

    # The inbound token *and* the upstream credential pair (key +
    # secret, plus any legacy 0.1.0 token still in the environment) are
    # registered as exact known secrets with the shared redaction
    # filter, so no log line can carry a raw credential.  The per-request
    # signature httpx echoes in its own INFO request lines is *not* an exact
    # known secret (it is computed per call), which is why
    # :func:`configure_logging` pins the httpx logger to WARNING.
    configure_logging(
        settings.log_level,
        secret_values=[*([settings.auth_token] if settings.auth_token else []),
                       *upstream.redaction_secrets()],
    )

    try:
        mcp_server = create_app(settings)
    except ConfigurationError as err:
        print(f"[http] configuration error: {err.describe()}", file=sys.stderr)
        flush_stderr()
        return 1
    except CoreError as err:
        print(f"[http] {err.describe()}", file=sys.stderr)
        flush_stderr()
        return 1
    except Exception as err:  # noqa: BLE001 - deliberate catch-all boundary
        print(f"[http] failed to start: {err}", file=sys.stderr)
        flush_stderr()
        return 1

    from cmdb_mcp_adapter.server.asgi import build_asgi_app
    from cmdb_mcp_adapter.server.health import UpstreamProber

    # Wire the readiness probe to a *real* (cheap, read-only) upstream
    # check when the veops adapter is in use, and always pass the upstream
    # settings so the readiness endpoint can report on them.  When the
    # adapter is not veops (e.g. the mock) we keep the shared no-op
    # prober so the readiness gate stays valid without extra I/O.
    prober: UpstreamProber | None = None
    if settings.adapter == "veops":
        from cmdb_mcp_adapter.adapters.veops import build_prober_from_upstream

        prober = build_prober_from_upstream(upstream)

    asgi_app = build_asgi_app(settings, mcp_server, prober=prober, upstream=upstream)

    # Install our signal handlers *before* uvicorn starts.  uvicorn's
    # Server.install_signal_handlers() is a no-op when it detects it is
    # running inside an existing event loop (which is our case — we call
    # ``server.serve()`` from inside ``asyncio.run``), so our handlers are
    # the only ones that fire.
    install_signal_handlers()

    exit_code = 0
    try:
        _serve_blocking(settings, asgi_app)
    except KeyboardInterrupt:
        # A SIGINT delivered before uvicorn's serve() had a chance to
        # observe the shutdown event (race window) raises
        # KeyboardInterrupt here.  Treat it as a signal-driven shutdown.
        exit_code = 130
    finally:
        restore_default_signals()
        if shutdown_event.is_set():
            # A SIGINT / SIGTERM was received; report a non-zero exit
            # code so the caller knows the process was interrupted, not
            # cleanly terminated.  This matches the stdio launcher's
            # behavior.
            exit_code = max(exit_code, 130)
        flush_stderr()
    return exit_code
