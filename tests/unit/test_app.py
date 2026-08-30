"""Composition-root tests: ``create_app`` must build a working server for
registered adapters and raise ``ConfigurationError`` for unknown ones.

Both transport launchers must go through ``create_app`` (asserted in
``tests/integration/test_transports.py``); this module tests the factory
itself.
"""

from __future__ import annotations

import asyncio

import pytest

from cmdb_mcp_adapter.config import Settings
from cmdb_mcp_adapter.core import (
    ConfigurationError,
    ErrorCategory,
)
from cmdb_mcp_adapter.server import app as app_module
from cmdb_mcp_adapter.server.app import create_app


class TestCreateApp:
    def test_mock_adapter_builds_server(self) -> None:
        settings = Settings(adapter="mock")
        server = create_app(settings)
        # The returned object is the FastMCP instance.  We assert it has the
        # expected shape: a low-level server with our registered handlers.
        assert server is not None
        # The low-level server must have the seven stable tool handlers.
        lowlevel = server._mcp_server
        assert "CallToolRequest" in [
            t.__name__ for t in lowlevel.request_handlers
        ]
        # The list_tools handler must advertise the seven tools.
        names = _list_tool_names(lowlevel)
        assert names == [
            "get_models",
            "get_instances",
            "create_instance",
            "update_instance",
            "delete_instance",
            "search_instances",
            "get_relations",
        ]

    def test_unknown_adapter_raises_configuration_error(self) -> None:
        settings = Settings(adapter="definitely-not-registered")
        with pytest.raises(ConfigurationError) as exc:
            create_app(settings)
        assert "definitely-not-registered" in str(exc.value)
        assert exc.value.category == ErrorCategory.CONFIGURATION

    def test_veops_registered_but_requires_config(self) -> None:
        # The veops adapter is now registered.  With ``VEOPS_BASE_URL`` /
        # ``VEOPS_KEY`` / ``VEOPS_SECRET`` unset (the conftest clean_env
        # fixture guarantees a clean environment) it is *not* an
        # unregistered-adapter error — it is the veops fail-closed policy
        # (missing base URL), which is still a ConfigurationError.
        settings = Settings(adapter="veops")
        with pytest.raises(ConfigurationError) as exc:
            create_app(settings)
        assert "VEOPS_BASE_URL" in str(exc.value)
        assert "not available in this build" not in str(exc.value)

    def test_register_adapter_adds_to_registry(self) -> None:
        from cmdb_mcp_adapter.adapters.mock import MockAdapter
        from cmdb_mcp_adapter.core import CMDBAdapter

        # Register a second factory under a new name and confirm create_app
        # picks it up.
        def factory(_settings: Settings) -> CMDBAdapter:
            return MockAdapter()

        app_module.register_adapter("test-alias", factory)
        try:
            settings = Settings(adapter="test-alias")
            server = create_app(settings)
            assert server is not None
        finally:
            # Clean up so other tests are not affected.
            app_module._ADAPTER_REGISTRY.pop("test-alias", None)


def _list_tool_names(lowlevel: object) -> list[str]:
    """Call the low-level server's ``list_tools`` handler with a dummy
    request and return the advertised tool names."""
    import mcp.types as types

    async def _invoke() -> list[str]:
        handler = lowlevel.request_handlers[types.ListToolsRequest]
        req = types.ListToolsRequest()
        result = await handler(req)  # type: ignore[call-arg]
        # result.root is a ListToolsResult with a .tools attribute.
        inner = result.root
        return [t.name for t in inner.tools]

    return asyncio.run(_invoke())

# ---------------------------------------------------------------------------
# Read-only mode (VEOPS_READONLY) through the composition root
# ---------------------------------------------------------------------------


class TestCreateAppReadOnly:
    def test_veops_readonly_hides_write_tools(self, monkeypatch) -> None:
        monkeypatch.setenv("VEOPS_BASE_URL", "http://cmdb.test:5000")
        monkeypatch.setenv("VEOPS_KEY", "k")
        monkeypatch.setenv("VEOPS_SECRET", "s")
        monkeypatch.setenv("VEOPS_READONLY", "1")
        settings = Settings(adapter="veops")
        server = create_app(settings)
        names = _list_tool_names(server._mcp_server)
        assert "create_instance" not in names
        assert "update_instance" not in names
        assert "delete_instance" not in names
        assert names == [
            "get_models",
            "get_instances",
            "search_instances",
            "get_relations",
        ]

    def test_veops_not_readonly_keeps_all_tools(self, monkeypatch) -> None:
        monkeypatch.setenv("VEOPS_BASE_URL", "http://cmdb.test:5000")
        monkeypatch.setenv("VEOPS_KEY", "k")
        monkeypatch.setenv("VEOPS_SECRET", "s")
        monkeypatch.delenv("VEOPS_READONLY", raising=False)
        settings = Settings(adapter="veops")
        server = create_app(settings)
        names = _list_tool_names(server._mcp_server)
        assert names == [
            "get_models",
            "get_instances",
            "create_instance",
            "update_instance",
            "delete_instance",
            "search_instances",
            "get_relations",
        ]

    def test_mock_ignores_veops_readonly_env(self, monkeypatch) -> None:
        # VEOPS_READONLY is a veops-domain switch: the mock adapter must
        # keep the full tool set even when the env flag is set.
        monkeypatch.setenv("VEOPS_READONLY", "1")
        settings = Settings(adapter="mock")
        server = create_app(settings)
        names = _list_tool_names(server._mcp_server)
        assert len(names) == 7

