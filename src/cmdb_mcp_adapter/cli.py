"""Command-line entry point for the CMDB MCP adapter.

Subcommands:
    stdio  Host-launched local process (stdio transport).
    http   Long-running Streamable HTTP server.

``--version``/``-V`` and ``--help`` work without any CMDB credentials or
network access.  The command layer never reads credentials directly; the
transport launchers obtain settings via :func:`cmdb_mcp_adapter.config.load_settings`.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable

from cmdb_mcp_adapter import __version__
from cmdb_mcp_adapter.config import apply_cli_overrides, load_settings


def _run_stdio() -> int:
    from cmdb_mcp_adapter.server.stdio import run_stdio

    # stdio has no --host/--port flags (it only speaks on stdio); the
    # settings come straight from the environment.
    return run_stdio(load_settings())


def _run_http(args: argparse.Namespace) -> int:
    """Run the http subcommand.

    ``args`` is the namespace produced by :func:`build_parser` and
    carries the (possibly ``None``) ``--host`` / ``--port`` values under
    ``_http_host`` / ``_http_port``.  Those are applied on top of the
    env-loaded settings, giving CLI > env > defaults precedence.
    """
    from cmdb_mcp_adapter.server.http import run_http

    settings = load_settings()
    settings = apply_cli_overrides(
        settings,
        host=args._http_host,
        port=args._http_port,
    )
    return run_http(settings)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cmdb-mcp",
        description="CMDB MCP adapter: expose CMDB resources via the Model Context Protocol.",
    )
    parser.add_argument(
        "--version",
        "-V",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command", required=False)

    stdio = subparsers.add_parser(
        "stdio",
        help="Launch the adapter as a host-launched stdio process (MCP stdio transport).",
        description="Launch the adapter as a host-launched stdio process. "
        "The MCP host starts this command and speaks JSON-RPC on stdin/stdout.",
    )
    stdio.set_defaults(func=_run_stdio)

    http = subparsers.add_parser(
        "http",
        help="Launch the adapter as a long-running Streamable HTTP server.",
        description=(
            "Launch the adapter as a long-running Streamable HTTP server "
            "(MCP Streamable HTTP transport).  By default it binds to "
            "MCP_BIND_HOST:MCP_PORT; the optional --host / --port flags "
            "override those environment values (CLI > env > defaults)."
        ),
    )
    http.add_argument(
        "--host",
        default=None,
        help="Bind address (overrides MCP_BIND_HOST).",
    )
    http.add_argument(
        "--port",
        type=int,
        default=None,
        help="Bind port (overrides MCP_PORT).",
    )
    http.set_defaults(func=_run_http, _http_host=None, _http_port=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "http":
        # Alias the public flag names to private keys so the http
        # launcher reads one stable attribute set.
        args._http_host = args.host
        args._http_port = args.port
    func: Callable[..., int] | None = getattr(args, "func", None)
    if func is None:
        # No subcommand given: show usage instead of silently exiting.
        parser.print_help()
        return 2
    if args.command == "http":
        return func(args)
    return func()


if __name__ == "__main__":
    sys.exit(main())
