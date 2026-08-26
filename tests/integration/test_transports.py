"""Both transport launchers must obtain the MCP server through the shared
composition root ``create_app`` — no transport may construct vendor
clients on its own.

This module tests the *wiring* of the launchers to ``create_app``.  The
stdio launcher runs a real stdio server; the HTTP launcher builds a real
ASGI app and runs it with uvicorn.  Configuration errors surface the same
way in both transports (non-zero exit, ``configuration`` on stderr).
"""

from __future__ import annotations

from unittest import mock

import pytest

from cmdb_mcp_adapter.config import Settings
from cmdb_mcp_adapter.core import ConfigurationError
from cmdb_mcp_adapter.server import http as http_launcher
from cmdb_mcp_adapter.server import stdio as stdio_launcher


class _FakeServer:
    """Stand-in for the MCP server object handed out by the composition
    root.  The stdio launcher calls ``run_stdio_async`` on it; we make
    that a no-op so the test completes quickly without any real IO."""

    async def run_stdio_async(self) -> None:  # pragma: no cover - test only
        return None


@pytest.fixture
def settings() -> Settings:
    return Settings(adapter="mock")


@pytest.fixture
def fake_server() -> _FakeServer:
    return _FakeServer()


def test_stdio_uses_create_app(
    settings: Settings,
    fake_server: _FakeServer,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with mock.patch.object(stdio_launcher, "create_app", return_value=fake_server) as spy:
        assert stdio_launcher.run_stdio(settings) == 0
    spy.assert_called_once_with(settings)
    out = capsys.readouterr()
    # The stdio launcher must not emit placeholder / not-implemented
    # notices once the server is built; it runs the stdio loop instead.
    assert "core contract not implemented yet" not in out.err
    assert "http transport not implemented yet" not in out.err


def test_http_uses_create_app(
    settings: Settings,
    fake_server: _FakeServer,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The HTTP launcher must call create_app with the settings.  We mock
    # build_asgi_app (so no real server is built from the fake) and
    # asyncio.run (so no real uvicorn server is started).  The test
    # completes quickly without binding a port.
    from cmdb_mcp_adapter.server import asgi as asgi_mod

    with mock.patch.object(http_launcher, "create_app", return_value=fake_server) as spy, \
         mock.patch.object(asgi_mod, "build_asgi_app", return_value=object()), \
         mock.patch.object(http_launcher, "install_signal_handlers"), \
         mock.patch.object(http_launcher, "restore_default_signals"), \
         mock.patch.object(http_launcher, "flush_stderr"), \
         mock.patch.object(http_launcher, "_serve_blocking"):
        result = http_launcher.run_http(settings)
    spy.assert_called_once_with(settings)
    out = capsys.readouterr()
    # The old placeholder notice must no longer appear.
    assert "http transport not implemented yet" not in out.err
    # A successful build + serve returns 0.
    assert result == 0


def test_stdio_configuration_error_path(
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A ConfigurationError from create_app must produce a non-zero exit
    # and a stderr message containing the category.
    with mock.patch.object(
        stdio_launcher, "create_app", side_effect=ConfigurationError("bad adapter")
    ):
        assert stdio_launcher.run_stdio(settings) != 0
    out = capsys.readouterr()
    assert "configuration" in out.err


def test_http_configuration_error_path(
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A ConfigurationError from create_app must surface on stderr and the
    # launcher must exit non-zero *before* building the ASGI app.
    with mock.patch.object(
        http_launcher, "create_app", side_effect=ConfigurationError("bad adapter")
    ):
        assert http_launcher.run_http(settings) != 0
    out = capsys.readouterr()
    assert "configuration" in out.err
    # The old placeholder notice must NOT be present.
    assert "http transport not implemented yet" not in out.err


def test_http_endpoint_constants() -> None:
    assert http_launcher.MCP_ENDPOINT == "/mcp"
    assert http_launcher.HEALTH_ENDPOINT == "/healthz"
    assert http_launcher.READY_ENDPOINT == "/readyz"
