"""Composition root for the CMDB MCP adapter.

Both the stdio and HTTP launchers obtain their server object from
:func:`create_app`.  No transport module may construct vendor clients or
duplicate core logic: :func:`create_app` is the single composition point that
builds the adapter (from a small registry), wraps it in a
:class:`~cmdb_mcp_adapter.core.CoreService`, and returns the shared MCP
server object.

The adapter registry is deliberately tiny.  The ``"mock"`` adapter is
registered on first use and the ``"veops"`` adapter is registered lazily
the first time that name is requested (see :func:`create_app`).  Adding a
further adapter is a one-line ``register_adapter`` call; no other code in
this package changes.
"""

from __future__ import annotations

from typing import Protocol

from cmdb_mcp_adapter.config import (
    Settings,
    load_upstream_settings,
    validate_settings,
    validate_upstream_settings,
)
from cmdb_mcp_adapter.core import (
    CMDBAdapter,
    ConfigurationError,
    CoreService,
)
from cmdb_mcp_adapter.server.mcp_server import build_mcp_server


class MCPServer(Protocol):
    """The object both transports consume.

    The SDK's ``FastMCP`` satisfies this protocol because it exposes
    ``run_stdio_async`` (used by the stdio launcher).  The future HTTP
    launcher will additionally use ``streamable_http_app``.  We keep the
    protocol minimal so the launchers do not depend on the concrete class.
    """

    async def run_stdio_async(self) -> None:  # pragma: no cover - protocol only
        ...


# The registry maps an adapter name (from ``Settings.adapter``) to a factory
# that builds an adapter instance.  Factories receive the ``Settings`` they
# were composed with so that vendor adapters can read their own config keys.
# Adding a new adapter is a one-line entry in this dict; no other code in
# this package changes.
_ADAPTER_REGISTRY: dict[str, object] = {}


def register_adapter(name: str, factory: object) -> None:
    """Register an adapter factory under ``name``.

    Intended for use by vendor adapter packages (the veops task will call
    this) and by tests.  The factory is invoked at :func:`create_app` time
    with the composed ``Settings``.
    """
    _ADAPTER_REGISTRY[name] = factory


def _build_adapter(settings: Settings) -> CMDBAdapter:
    factory = _ADAPTER_REGISTRY.get(settings.adapter)
    if factory is None:
        known = ", ".join(sorted(_ADAPTER_REGISTRY)) or "(none registered)"
        raise ConfigurationError(
            f"adapter '{settings.adapter}' is not available in this build; "
            f"known adapters: {known}"
        )
    # The factory is a callable that accepts Settings and returns an adapter.
    adapter: CMDBAdapter = factory(settings)  # type: ignore[operator]
    return adapter


def create_app(settings: Settings) -> MCPServer:
    """Build the shared MCP server object from validated settings.

    This is the single composition point for every transport.  It does not
    perform any network I/O at construction time; the adapter is only
    contacted when a tool is actually invoked.

    Startup validation (fail closed) is performed here, before any
    adapter is constructed:

    * a non-loopback ``bind_host`` requires ``MCP_AUTH_TOKEN`` to be
      configured (see :func:`cmdb_mcp_adapter.config.validate_settings`);
    * the requested adapter name must be registered in this build.

    Raises:
        ConfigurationError: if any of the above checks fails.  The
            transport launchers surface this on ``stderr`` and exit
            non-zero before any protocol activity.
    """
    # Fail closed on inbound-config errors before touching the adapter
    # registry; this is the single point where both transports are
    # validated, so the http and stdio launchers share one behavior.
    validate_settings(settings)
    # Also fail closed on an invalid upstream (VEOPS_*) configuration:
    # a misconfigured CMDB endpoint is a startup error in both transports.
    upstream = load_upstream_settings()
    validate_upstream_settings(upstream)

    # Register the mock adapter on first call so that tests and local
    # development can use it without importing the adapters package directly.
    if "mock" not in _ADAPTER_REGISTRY:
        from cmdb_mcp_adapter.adapters.mock import MockAdapter

        def _mock_factory(_settings: Settings) -> CMDBAdapter:
            return MockAdapter()

        register_adapter("mock", _mock_factory)

    # Register the veops adapter on first *use*.  The import is deferred to
    # this branch (rather than at module import time) so that:
    #   * the composition root never imports a vendor adapter at startup
    #     (keeps ``import cmdb_mcp_adapter.server.app`` free of veops
    #     dependencies, and avoids any import cycle); and
    #   * the veops factory is only constructed when ``MCP_ADAPTER=veops``
    #     is actually requested.
    if "veops" not in _ADAPTER_REGISTRY:
        from cmdb_mcp_adapter.adapters.veops import create_veops_adapter

        register_adapter("veops", create_veops_adapter)

    adapter = _build_adapter(settings)
    service = CoreService(adapter)
    # The VEOPS_READONLY switch is a veops-domain flag: it only hides the
    # mutation tools when the composed adapter is the veops one.  The mock
    # adapter (contract tests / local dev) always keeps the full tool set
    # regardless of the env value.
    server = build_mcp_server(
        settings,
        service,
        readonly=(settings.adapter == "veops" and upstream.readonly),
    )
    # FastMCP satisfies the MCPServer protocol at runtime; the cast is a
    # compile-time convenience for the launchers.
    return server
