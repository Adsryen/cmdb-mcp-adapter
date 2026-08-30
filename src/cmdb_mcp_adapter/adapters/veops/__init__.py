"""The VeOps CMDB adapter package.

Public entry point: :func:`create_veops_adapter` builds a
:class:`VeopsAdapter` from the inbound :class:`Settings` (reading the
upstream ``VEOPS_*`` values and the shared HTTP timeout / retry budget).
The adapter is registered into the composition root under the name
``"veops"`` the first time :func:`cmdb_mcp_adapter.server.app.create_app`
resolves that name, so ``MCP_ADAPTER=veops`` becomes usable without any
import cycle (the composition root lazy-imports this module).

The adapter targets the **verified OneOps/VeOps 2.6.2 contract**:
``/api/v0.1`` routes with per-request key/secret SHA-1 signing
(see :mod:`.signing` and ``docs/veops-adapter.md``).
"""

from __future__ import annotations

from cmdb_mcp_adapter.adapters.veops.adapter import VeopsAdapter
from cmdb_mcp_adapter.adapters.veops.client import VeopsClient
from cmdb_mcp_adapter.adapters.veops.config import (
    API_PREFIX,
    VeopsConfig,
    build_veops_config,
    validate_veops_upstream,
)
from cmdb_mcp_adapter.adapters.veops.prober import (
    build_prober_from_upstream,
    make_veops_prober,
)
from cmdb_mcp_adapter.config import Settings, load_upstream_settings
from cmdb_mcp_adapter.core import CMDBAdapter


def create_veops_adapter(settings: Settings) -> CMDBAdapter:
    """Factory used by the composition root (registered as ``"veops"``).

    Reads the upstream ``VEOPS_*`` settings from the environment,
    validates the veops-specific policy (fail closed: base URL **and**
    the key/secret credential pair are mandatory), and builds a
    transport-backed :class:`VeopsAdapter` whose HTTP timeout and retry
    budget come from the inbound :class:`Settings`.  No network I/O
    happens here; the first upstream call only occurs when a tool is
    actually invoked.
    """
    upstream = load_upstream_settings()
    config = build_veops_config(
        upstream,
        timeout_seconds=settings.http_timeout_seconds,
        max_retries=settings.http_max_retries,
    )
    client = VeopsClient(config)
    return VeopsAdapter(
        client,
        max_retries=settings.http_max_retries,
        readonly=upstream.readonly,
    )


__all__ = [
    "API_PREFIX",
    "VeopsAdapter",
    "VeopsClient",
    "VeopsConfig",
    "build_prober_from_upstream",
    "build_veops_config",
    "create_veops_adapter",
    "make_veops_prober",
    "validate_veops_upstream",
]
