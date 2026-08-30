"""Contract tests for the MCP tool layer.

These tests drive the real MCP server (built by
:func:`cmdb_mcp_adapter.server.mcp_server.build_mcp_server`) through the SDK's
in-memory client session and assert:

* the seven stable tool names are registered;
* ``get_models`` returns a non-empty, parseable result;
* ``get_instances`` pagination semantics are preserved over the wire;
* error tool calls return ``isError=True`` with a category-labelled message
  and never leak ``upstream_detail``.

The tests use the official MCP Python SDK's in-memory transport
(``mcp.shared.memory.create_connected_server_and_client_session``), which
runs a real JSON-RPC session without any network I/O.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from cmdb_mcp_adapter.adapters.mock import MockAdapter
from cmdb_mcp_adapter.config import Settings
from cmdb_mcp_adapter.core import READONLY_TOOL_NAMES, CoreService
from cmdb_mcp_adapter.server.mcp_server import build_mcp_server

EXPECTED_TOOL_NAMES = [
    "get_models",
    "get_instances",
    "create_instance",
    "update_instance",
    "delete_instance",
    "search_instances",
    "get_relations",
]


def _service_for(adapter: MockAdapter) -> CoreService:
    return CoreService(adapter)


def _build(adapter: MockAdapter) -> Any:
    return build_mcp_server(Settings(adapter="mock"), _service_for(adapter))


def _build_veops_adapter() -> Any:
    """Build a VeopsAdapter with an injected mock transport (no network)."""
    from cmdb_mcp_adapter.adapters.veops import VeopsAdapter
    from tests.adapters import _fakes

    transport = _fakes._RecordingTransport(
        {("GET", "/api/v0.1/ci_types"): {"ci_types": []}},
        {},
    )
    client = _fakes.build_client(transport)
    return VeopsAdapter(client, max_retries=2)


@pytest.fixture
def default_server() -> Any:
    return _build(MockAdapter())


def _run(coro: Any) -> Any:
    """Run an async coroutine to completion on a fresh event loop."""
    return asyncio.run(coro)


@asynccontextmanager
async def _session(server: Any) -> AsyncIterator[Any]:
    from mcp.shared.memory import create_connected_server_and_client_session

    async with create_connected_server_and_client_session(
        server, raise_exceptions=False
    ) as cs:
        yield cs


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


class TestToolRegistration:
    def test_all_seven_tools_registered(self, default_server: Any) -> None:
        async def check() -> list[str]:
            async with _session(default_server) as cs:
                tools = await cs.list_tools()
                return [t.name for t in tools.tools]

        names = _run(check())
        assert names == EXPECTED_TOOL_NAMES

    def test_tool_schemas_are_present(self, default_server: Any) -> None:
        async def check() -> None:
            async with _session(default_server) as cs:
                tools = await cs.list_tools()
                for t in tools.tools:
                    assert t.inputSchema is not None
                    assert t.inputSchema.get("type") == "object"

        _run(check())

    def test_veops_adapter_tool_list_matches_frozen_contract(self) -> None:
        """The veops adapter must expose the exact same frozen tool list
        (names + JSON input schemas) as the mock adapter.  The transport
        is injected; no network or credentials are needed."""

        async def check() -> None:
            veops_adapter = _build_veops_adapter()
            veops_server = build_mcp_server(
                Settings(adapter="veops"), _service_for(veops_adapter)
            )
            async with _session(veops_server) as cs:
                veops_tools = await cs.list_tools()
            async with _session(_build(MockAdapter())) as cs:
                mock_tools = await cs.list_tools()
            assert [t.name for t in veops_tools.tools] == [
                t.name for t in mock_tools.tools
            ] == EXPECTED_TOOL_NAMES
            # The JSON input schemas are field-for-field identical.
            for vt, mt in zip(veops_tools.tools, mock_tools.tools, strict=True):
                assert vt.name == mt.name
                assert vt.inputSchema == mt.inputSchema

        _run(check())




class TestGetModelsTool:
    def test_returns_non_empty(self, default_server: Any) -> None:
        async def check() -> None:
            async with _session(default_server) as cs:
                r = await cs.call_tool("get_models", {})
                assert r.isError is False
                data = json.loads(r.content[0].text)
                assert isinstance(data, list) and len(data) >= 1
                assert {m["name"] for m in data} == {"host", "service"}

        _run(check())


# ---------------------------------------------------------------------------
# get_instances pagination
# ---------------------------------------------------------------------------


class TestGetInstancesTool:
    def test_pagination_across_pages(self, default_server: Any) -> None:
        async def check() -> None:
            async with _session(default_server) as cs:
                p1 = json.loads(
                    (await cs.call_tool(
                        "get_instances",
                        {"model_name": "host", "page_size": 3},
                    )).content[0].text
                )
                assert p1["total"] == 8
                assert len(p1["items"]) == 3
                assert p1["has_next"] is True
                assert p1["next_cursor"] == "2"

                p2 = json.loads(
                    (await cs.call_tool(
                        "get_instances",
                        {"model_name": "host", "page_size": 3, "cursor": "2"},
                    )).content[0].text
                )
                assert [i["instance_id"] for i in p2["items"]] == [
                    "host-4",
                    "host-5",
                    "host-6",
                ]
                assert p2["next_cursor"] == "3"

                p3 = json.loads(
                    (await cs.call_tool(
                        "get_instances",
                        {"model_name": "host", "page_size": 3, "cursor": "3"},
                    )).content[0].text
                )
                assert [i["instance_id"] for i in p3["items"]] == [
                    "host-7",
                    "host-8",
                ]
                assert p3["has_next"] is False
                assert p3["next_cursor"] is None

        _run(check())

    def test_validation_error_over_wire(self, default_server: Any) -> None:
        async def check() -> None:
            async with _session(default_server) as cs:
                r = await cs.call_tool("get_instances", {"model_name": ""})
                assert r.isError is True
                assert r.content[0].text.startswith("validation:")

        _run(check())


# ---------------------------------------------------------------------------
# Mutation tools
# ---------------------------------------------------------------------------


class TestMutationTools:
    def test_create_and_update_and_delete(self, default_server: Any) -> None:
        async def check() -> None:
            async with _session(default_server) as cs:
                created = json.loads(
                    (await cs.call_tool(
                        "create_instance",
                        {"model_name": "service", "data": {"name": "x", "env": "dev"}},
                    )).content[0].text
                )
                assert created["model_name"] == "service"
                new_id = created["instance_id"]

                updated = json.loads(
                    (await cs.call_tool(
                        "update_instance",
                        {"model_name": "service", "instance_id": new_id,
                         "data": {"env": "prod"}},
                    )).content[0].text
                )
                assert updated["data"]["env"] == "prod"

                deleted = await cs.call_tool(
                    "delete_instance",
                    {"model_name": "service", "instance_id": new_id},
                )
                assert deleted.isError is False

        _run(check())


# ---------------------------------------------------------------------------
# search / relations
# ---------------------------------------------------------------------------


class TestSearchAndRelations:
    def test_search(self, default_server: Any) -> None:
        async def check() -> None:
            async with _session(default_server) as cs:
                r = await cs.call_tool(
                    "search_instances",
                    {"model_name": "host", "query": "web-1", "page_size": 10},
                )
                assert r.isError is False
                data = json.loads(r.content[0].text)
                assert data["total"] >= 1

        _run(check())

    def test_relations(self, default_server: Any) -> None:
        async def check() -> None:
            async with _session(default_server) as cs:
                r = await cs.call_tool(
                    "get_relations",
                    {"model_name": "host", "instance_id": "host-1"},
                )
                assert r.isError is False
                data = json.loads(r.content[0].text)
                assert len(data) == 1
                assert data[0]["relation_type"] == "runs"

        _run(check())


# ---------------------------------------------------------------------------
# Error mapping over the wire
# ---------------------------------------------------------------------------


class TestErrorMapping:
    def test_unsupported_operation_over_wire(self) -> None:
        server = _build(MockAdapter(unsupported={"get_relations": "nope"}))

        async def check() -> None:
            async with _session(server) as cs:
                r = await cs.call_tool(
                    "get_relations",
                    {"model_name": "host", "instance_id": "host-1"},
                )
                assert r.isError is True
                assert r.content[0].text.startswith("unsupported_operation:")

        _run(check())

    def test_upstream_error_category_preserved_and_no_leak(self) -> None:
        from cmdb_mcp_adapter.core import RateLimitError

        server = _build(
            MockAdapter(
                error_injection={
                    "*": RateLimitError("throttled", upstream_detail="RAW-SECRET")
                }
            )
        )

        async def check() -> None:
            async with _session(server) as cs:
                r = await cs.call_tool("get_models", {})
                assert r.isError is True
                assert r.content[0].text.startswith("rate_limit:")
                # The upstream detail must never leak to the client.
                assert "RAW-SECRET" not in r.content[0].text

        _run(check())

    def test_unknown_tool(self, default_server: Any) -> None:
        async def check() -> None:
            async with _session(default_server) as cs:
                r = await cs.call_tool("does-not-exist", {})
                assert r.isError is True

        _run(check())

# ---------------------------------------------------------------------------
# Read-only mode (VEOPS_READONLY): the three mutation tools disappear from
# the advertised list and cannot be invoked.
# ---------------------------------------------------------------------------


class TestReadOnlyMode:
    def _build_readonly(self, adapter: MockAdapter) -> Any:
        return build_mcp_server(
            Settings(adapter="veops"), CoreService(adapter), readonly=True
        )

    def test_list_tools_hides_write_tools(self) -> None:
        server = self._build_readonly(MockAdapter())

        async def check() -> list[str]:
            async with _session(server) as cs:
                tools = await cs.list_tools()
                return [t.name for t in tools.tools]

        check_run = _run(check())
        assert check_run == list(READONLY_TOOL_NAMES)
        assert "create_instance" not in check_run
        assert "update_instance" not in check_run
        assert "delete_instance" not in check_run
        assert len(check_run) == 4

    def test_read_tool_still_works(self) -> None:
        server = self._build_readonly(MockAdapter())

        async def check() -> bool:
            async with _session(server) as cs:
                r = await cs.call_tool("get_models", {})
                return r.isError is False

        assert _run(check()) is True

    def test_write_tool_call_rejected(self) -> None:
        server = self._build_readonly(MockAdapter())

        async def check() -> bool:
            async with _session(server) as cs:
                r = await cs.call_tool(
                    "create_instance",
                    {"model_name": "service", "data": {"name": "x"}},
                )
                return r.isError is True

        assert _run(check()) is True

    def test_default_keeps_all_seven_tools(self) -> None:
        # The default (write-enabled) build must still advertise all seven.
        server = build_mcp_server(
            Settings(adapter="veops"), CoreService(MockAdapter())
        )

        async def check() -> list[str]:
            async with _session(server) as cs:
                tools = await cs.list_tools()
                return [t.name for t in tools.tools]

        assert _run(check()) == EXPECTED_TOOL_NAMES

