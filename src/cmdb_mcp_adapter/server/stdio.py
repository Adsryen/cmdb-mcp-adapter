"""Stdio transport launcher (host-launched local process).

The launcher only configures lifecycle and IO; the MCP server itself comes
from :func:`cmdb_mcp_adapter.server.app.create_app`.  ``stdout`` is reserved
for MCP protocol frames; all diagnostics (including configuration errors)
go to ``stderr`` as structured JSON lines (see
:mod:`cmdb_mcp_adapter.observability`).
"""

from __future__ import annotations

import asyncio
import sys

from cmdb_mcp_adapter.config import Settings
from cmdb_mcp_adapter.core import ConfigurationError, CoreError
from cmdb_mcp_adapter.observability.logging import configure_logging
from cmdb_mcp_adapter.server.app import create_app
from cmdb_mcp_adapter.server.lifecycle import (
    flush_stderr,
    install_signal_handlers,
    restore_default_signals,
)


def run_stdio(settings: Settings) -> int:
    """Run the shared MCP server over stdio.

    Configuration errors are reported on ``stderr`` with a non-zero exit
    code before any protocol frame is written to ``stdout``.  Once the
    server starts, it runs the SDK's stdio loop until the host closes
    stdin (``Ctrl-C`` / ``SIGTERM`` also terminate cleanly and the stderr
    stream is flushed before exit).
    """
    from cmdb_mcp_adapter.config import load_upstream_settings

    # Inbound token plus the upstream credential pair (key/secret/
    # legacy token) are exact known secrets for the shared redaction
    # filter: no log line may carry a raw credential value.
    upstream = load_upstream_settings()
    configure_logging(
        settings.log_level,
        secret_values=[*([settings.auth_token] if settings.auth_token else []),
                       *upstream.redaction_secrets()],
    )
    try:
        app = create_app(settings)
    except ConfigurationError as err:
        # Never write to stdout: the host is still waiting for a valid
        # response and any stray bytes would corrupt the protocol stream.
        print(f"[stdio] configuration error: {err.describe()}", file=sys.stderr)
        flush_stderr()
        return 1
    except CoreError as err:
        print(f"[stdio] {err.describe()}", file=sys.stderr)
        flush_stderr()
        return 1
    except Exception as err:  # noqa: BLE001 - deliberate catch-all boundary
        print(f"[stdio] failed to start: {err}", file=sys.stderr)
        flush_stderr()
        return 1

    # Install graceful-shutdown handlers for SIGINT / SIGTERM; the SDK
    # stdio loop still owns the main thread, so the handlers only set an
    # event that the loop (or the next host message) observes.
    install_signal_handlers()
    try:
        asyncio.run(app.run_stdio_async())
    except KeyboardInterrupt:
        # SIGINT while the stdio loop is running: exit cleanly.  Nothing is
        # written to stdout so the protocol stream stays well-formed.
        print("[stdio] interrupted", file=sys.stderr)
        flush_stderr()
        return 130
    finally:
        restore_default_signals()
        # Ensure any trailing structured log lines are flushed before the
        # process exits so the host's log aggregator does not lose them.
        flush_stderr()
    return 0
