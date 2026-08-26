"""Transport-agnostic MCP tool registration for the unified CMDB core.

This module owns the *contract* between the MCP protocol and the core
service.  It builds a low-level ``mcp.server.Server`` with exactly the seven
stable tools named in :mod:`cmdb_mcp_adapter.core` and returns it wrapped in a
:class:`FastMCP` instance so that both the stdio launcher and (later) the
Streamable HTTP launcher can consume the same object through the SDK's
standard ``run`` / ``run_stdio_async`` entry points.

Design notes
------------
* The SDK's low-level ``Server.call_tool`` registers a **single**
  ``CallToolRequest`` handler; each new registration replaces the previous
  one.  We therefore register exactly one *dispatcher* handler that looks up
  the per-tool coroutine from a dictionary keyed by tool name and invokes it.
* The dispatcher uses ``validate_input=False`` so that :class:`CoreService`
  owns the contract validation rules.  The SDK's JSON-schema check is skipped
  in favour of our richer, category-labelled error messages.
* Every handler returns a :class:`mcp.types.CallToolResult`.  On success
  ``isError=False`` and the ``content`` list holds a single
  :class:`TextContent` with the JSON-serialized normalized model.  On
  failure ``isError=True`` and the text is exactly ``<category>: <message>``
  — the raw ``upstream_detail`` is never emitted to the client.
* The ``list_tools`` handler advertises the seven tools with explicit JSON
  schemas so that MCP clients can introspect the contract without calling
  ``get_models``.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict, is_dataclass
from typing import Any

from mcp import types
from mcp.server import Server
from mcp.server.fastmcp import FastMCP
from mcp.types import (
    CallToolRequest,
    CallToolResult,
    ListToolsRequest,
    ListToolsResult,
    ServerResult,
    TextContent,
    Tool,
)

from cmdb_mcp_adapter.config import Settings
from cmdb_mcp_adapter.core import (
    ALL_TOOL_NAMES,
    CoreError,
    CoreService,
    normalize,
)
from cmdb_mcp_adapter.observability.correlation import bind_correlation_id, current_correlation_id

#: The advertised server name during MCP initialization.  This is *not* a
#: tool name and may change without breaking the tool contract.
SERVER_NAME = "cmdb-mcp-adapter"

#: Explicit JSON input schemas for the seven tools.  These mirror the
#: ``CoreService`` method signatures and are the client-visible contract.
_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "get_models": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    "get_instances": {
        "type": "object",
        "properties": {
            "model_name": {"type": "string"},
            "page_size": {"type": "integer", "minimum": 1, "default": 50},
            "cursor": {"type": ["string", "null"]},
            "filters": {
                "type": "object",
                "additionalProperties": {"type": ["string", "integer", "boolean", "null"]},
            },
        },
        "required": ["model_name"],
    },
    "create_instance": {
        "type": "object",
        "properties": {
            "model_name": {"type": "string"},
            "data": {"type": "object"},
        },
        "required": ["model_name", "data"],
    },
    "update_instance": {
        "type": "object",
        "properties": {
            "model_name": {"type": "string"},
            "instance_id": {"type": "string"},
            "data": {"type": "object"},
        },
        "required": ["model_name", "instance_id", "data"],
    },
    "delete_instance": {
        "type": "object",
        "properties": {
            "model_name": {"type": "string"},
            "instance_id": {"type": "string"},
        },
        "required": ["model_name", "instance_id"],
    },
    "search_instances": {
        "type": "object",
        "properties": {
            "model_name": {"type": "string"},
            "query": {"type": "string"},
            "page_size": {"type": "integer", "minimum": 1, "default": 50},
            "cursor": {"type": ["string", "null"]},
        },
        "required": ["model_name", "query"],
    },
    "get_relations": {
        "type": "object",
        "properties": {
            "model_name": {"type": "string"},
            "instance_id": {"type": "string"},
        },
        "required": ["model_name", "instance_id"],
    },
}

_TOOL_DESCRIPTIONS: dict[str, str] = {
    "get_models": "List the CMDB models this adapter exposes.",
    "get_instances": "List one page of instances of a model (cursor-paginated).",
    "create_instance": "Create a new instance of a model.",
    "update_instance": "Update an existing instance of a model.",
    "delete_instance": "Delete an existing instance of a model.",
    "search_instances": "Free-text search over a model's instances (cursor-paginated).",
    "get_relations": "List the relations originating from an instance.",
}


def _to_jsonable(value: Any) -> Any:
    """Recursively convert a normalized model into plain JSON-able data.

    For :class:`Page` we add the computed ``has_next`` field to the
    serialized output so that clients get the convenience boolean alongside
    the raw ``next_cursor``.
    """
    if is_dataclass(value) and not isinstance(value, type):
        out = _to_jsonable(asdict(value))
        # Page carries a derived ``has_next`` property that ``asdict`` does
        # not include; add it explicitly for client convenience.
        if hasattr(value, "has_next"):
            out["has_next"] = bool(value.has_next)
        return out
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    return value


def _to_text(value: Any) -> str:
    """Serialize a normalized value to a JSON string for MCP text content."""
    if isinstance(value, str):
        return value
    return json.dumps(_to_jsonable(value), indent=2, ensure_ascii=False)


def _ok_result(text: str) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        isError=False,
    )


def _error_result(err: CoreError) -> CallToolResult:
    """Build an MCP ``CallToolResult`` that signals an error to the client.

    The text is ``<category>: <message>``.  ``upstream_detail`` is never
    included, so vendor internals / credentials never reach the client.
    """
    return CallToolResult(
        content=[TextContent(type="text", text=err.describe())],
        isError=True,
    )


# A per-tool handler is a coroutine taking the incoming arguments dict and
# returning a CallToolResult.  The dispatcher wraps each one in a try/except
# that converts any exception into an error result.
Handler = Callable[[dict[str, Any]], Awaitable[CallToolResult]]


def _wrap(handler: Handler, tool_name: str) -> Handler:
    """Wrap a per-tool handler so any exception becomes a clean MCP error.

    ``tool_name`` is the stable MCP tool name this handler serves; it is
    passed in explicitly (rather than recovered from a side-table) so
    the correlation log line always carries the public contract name.

    Each invocation is assigned a fresh correlation ID (via
    :func:`log_tool_invoked`) which is stored in the
    :data:`current_correlation_id` context variable so that the core
    service's :func:`log_operation` boundary can pick it up and log the
    same ID on the matching ``operation.start`` / ``operation.end``
    lines.  The tool input/output contract is unchanged: the wrapper
    only *adds* a log line and a context binding, it does not read or
    transform ``arguments`` or the handler's result.
    """

    async def wrapped(arguments: dict[str, Any]) -> CallToolResult:
        # Bind a per-invocation correlation ID.  We deliberately do not
        # reset it here: the SDK runs the handler on the same task, and
        # the core service's contextvar binding inside ``log_operation``
        # (which creates a nested ``with_correlation``) inherits this ID
        # and restores it on exit.  For the stdio transport the task is
        # not reused across calls, so the binding is scoped to one call.
        token = bind_correlation_id()
        try:
            return await handler(arguments)
        except CoreError as err:
            return _error_result(err)
        except Exception as err:  # noqa: BLE001 - deliberate catch-all boundary
            return _error_result(normalize(err))
        finally:
            current_correlation_id.reset(token)

    return wrapped


def _current_tool_name(handler: Handler) -> str:
    """Recover the tool name a handler wraps from the build-time side-table.

    :func:`build_mcp_server` records ``id(handler) -> tool_name`` for
    every handler it creates, so this lookup is O(1) and never falls
    back to a guess at runtime (the only way to reach ``"unknown"`` is
    a handler that was never registered by the build, which would also
    mean the dispatcher cannot route it).
    """
    return _handler_tool_names.get(id(handler), "unknown")


def adapter_name_for(handler: Handler) -> str:
    """Adapter name associated with the current build (for log lines).

    The adapter is owned by the :class:`CoreService`; the tool layer
    only has a reference to the service it was composed with.  We read
    the service's adapter type name (a stable, non-secret label) as the
    ``adapter`` field on the log line.
    """
    return _service_adapter_label


#: Side-tables populated by :func:`build_mcp_server` so the ``_wrap``
#: boundary can label each log line with the tool name it wraps and the
#: adapter name of the service it was composed with.  These are
#: per-build globals (the server object is built once per process) and
#: are not part of the MCP contract.
_handler_tool_names: dict[int, str] = {}
_service_adapter_label: str = "unknown"


def build_mcp_server(settings: Settings, service: CoreService) -> FastMCP:

    """Build the transport-agnostic MCP server with the seven core tools.

    The returned :class:`FastMCP` is the *single* shared object both the
    stdio and (future) HTTP transports consume.  Construction does not
    perform any network I/O; the adapter is only touched when a tool is
    actually called.
    """
    del settings  # The server is vendor-neutral; the adapter is already in service.
    lowlevel = Server(
        name=SERVER_NAME,
        instructions=(
            "Unified CMDB adapter. The seven tool names below are a stable "
            "public contract and must not change when new vendor adapters "
            "are added."
        ),
    )

    # ---- list_tools: advertise the seven stable tools --------------------
    # We register the raw handler directly on ``request_handlers`` rather than
    # through the ``Server.list_tools`` decorator because that decorator is
    # untyped in the SDK and would trip ``mypy --strict``.
    async def _list_tools_handler(_req: ListToolsRequest) -> ServerResult:
        tools = [
            Tool(
                name=name,
                description=_TOOL_DESCRIPTIONS.get(name, ""),
                inputSchema=_TOOL_SCHEMAS[name],
            )
            for name in ALL_TOOL_NAMES
        ]
        return ServerResult(ListToolsResult(tools=tools))

    lowlevel.request_handlers[types.ListToolsRequest] = _list_tools_handler

    # ---- helper: run a synchronous CoreService call in a thread ----------
    async def _run_sync(fn: Callable[[], Any]) -> Any:
        # asyncio.to_thread runs the callable in the default executor thread
        # pool so the event loop is never blocked by long-running upstream
        # HTTP calls.  For the synchronous mock adapter this is negligible
        # overhead.  We copy the current context (which carries the
        # correlation-ID contextvar set by the ``_wrap`` boundary) into
        # the worker thread so the core service's ``log_operation`` call
        # sees the same ID and the tool/operation log lines correlate.
        ctx = contextvars.copy_context()
        return await asyncio.to_thread(ctx.run, fn)

    # ---- per-tool handlers ------------------------------------------------
    async def _get_models(arguments: dict[str, Any]) -> CallToolResult:
        del arguments
        result = await _run_sync(lambda: service.list_models())
        return _ok_result(_to_text(result))

    async def _get_instances(arguments: dict[str, Any]) -> CallToolResult:
        result = await _run_sync(
            lambda: service.get_instances(
                model_name=arguments.get("model_name", ""),
                page_size=int(arguments.get("page_size", 50)),
                cursor=arguments.get("cursor"),
                filters=arguments.get("filters") or {},
            )
        )
        return _ok_result(_to_text(result))

    async def _create_instance(arguments: dict[str, Any]) -> CallToolResult:
        result = await _run_sync(
            lambda: service.create_instance(
                model_name=arguments.get("model_name", ""),
                data=arguments.get("data") or {},
            )
        )
        return _ok_result(_to_text(result))

    async def _update_instance(arguments: dict[str, Any]) -> CallToolResult:
        result = await _run_sync(
            lambda: service.update_instance(
                model_name=arguments.get("model_name", ""),
                instance_id=arguments.get("instance_id", ""),
                data=arguments.get("data") or {},
            )
        )
        return _ok_result(_to_text(result))

    async def _delete_instance(arguments: dict[str, Any]) -> CallToolResult:
        await _run_sync(
            lambda: service.delete_instance(
                model_name=arguments.get("model_name", ""),
                instance_id=arguments.get("instance_id", ""),
            )
        )
        return _ok_result("deleted")

    async def _search_instances(arguments: dict[str, Any]) -> CallToolResult:
        result = await _run_sync(
            lambda: service.search_instances(
                model_name=arguments.get("model_name", ""),
                query=arguments.get("query", ""),
                page_size=int(arguments.get("page_size", 50)),
                cursor=arguments.get("cursor"),
            )
        )
        return _ok_result(_to_text(result))

    async def _get_relations(arguments: dict[str, Any]) -> CallToolResult:
        result = await _run_sync(
            lambda: service.get_relations(
                model_name=arguments.get("model_name", ""),
                instance_id=arguments.get("instance_id", ""),
            )
        )
        return _ok_result(_to_text(result))

    handlers: dict[str, Handler] = {
        "get_models": _wrap(_get_models, "get_models"),
        "get_instances": _wrap(_get_instances, "get_instances"),
        "create_instance": _wrap(_create_instance, "create_instance"),
        "update_instance": _wrap(_update_instance, "update_instance"),
        "delete_instance": _wrap(_delete_instance, "delete_instance"),
        "search_instances": _wrap(_search_instances, "search_instances"),
        "get_relations": _wrap(_get_relations, "get_relations"),
    }

    # ---- single dispatcher handler (the SDK allows only one) -------------
    # We implement the CallToolRequest handler by hand rather than going
    # through the ``Server.call_tool`` decorator: that decorator is untyped in
    # the SDK (trips ``mypy --strict``) and it hard-codes
    # ``validate_input=True``.  Our raw handler disables JSON-schema
    # validation so that :class:`CoreService` owns the contract rules, and it
    # routes each request to the matching per-tool coroutine by name.
    async def _dispatch(req: CallToolRequest) -> ServerResult:
        tool_name = req.params.name
        arguments: dict[str, Any] = req.params.arguments or {}
        handler = handlers.get(tool_name)
        if handler is None:
            return ServerResult(
                _error_result(
                    normalize(
                        CoreError(
                            f"unknown tool '{tool_name}'; expected one of {sorted(handlers)}"
                        )
                    )
                )
            )
        return ServerResult(await handler(arguments))

    lowlevel.request_handlers[types.CallToolRequest] = _dispatch

    # Record the tool-name -> handler mapping and the adapter label so
    # the ``_wrap`` boundary can log structured fields.  These are
    # module-level globals (the server is built once per process).
    global _service_adapter_label
    _service_adapter_label = type(service.adapter).__name__
    # The side-table is no longer needed for the wrap path (the tool
    # name is passed explicitly); we still record it so any future
    # code that has a reference to a wrapped handler can recover its
    # name without re-reading the dispatcher dict.
    for _name, _h in handlers.items():
        _handler_tool_names[id(_h)] = _name

    # ---- wrap in FastMCP for the transports -------------------------------
    # FastMCP's constructor builds its own low-level Server; we replace it
    # with ours so that ``run_stdio_async`` / ``streamable_http_app`` call
    # *our* registered handlers.  This mirrors the pattern the SDK's
    # in-memory test helper uses (see ``mcp.shared.memory``).
    fastmcp = FastMCP(name=SERVER_NAME, instructions=lowlevel.instructions)
    fastmcp._mcp_server = lowlevel
    return fastmcp
