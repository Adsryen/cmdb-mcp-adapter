"""Integration tests for the Streamable HTTP transport.

The ASGI app is exercised *in-process* through ``httpx.ASGITransport`` so
no port is bound: the SDK's MCP client (``streamable_http_client`` +
``ClientSession``) drives the real ``StreamableHTTPSessionManager`` that
:func:`cmdb_mcp_adapter.server.asgi.build_asgi_app` builds around the
shared server object from ``create_app``.

Covered:

* the full MCP protocol handshake (initialize → tools/list → tools/call)
  over ``POST /mcp``, asserting the fixed stable tool names;
* the inbound auth policy (``0.0.0.0`` + token → 401 without/with a wrong
  token; correct token → 200; loopback without token → 200);
* ``/healthz`` (always 200) and ``/readyz`` (200 valid / 503 with redacted
  reasons for a bad configuration or a failing prober);
* unknown paths → JSON 404;
* a real-process end-to-end test (uvicorn on a real port, ``SIGINT``)
  covering graceful shutdown and the stdout-cleanliness contract.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from typing import Any

import httpx
import pytest

from cmdb_mcp_adapter.config import Settings, UpstreamSettings
from cmdb_mcp_adapter.server.app import create_app
from cmdb_mcp_adapter.server.asgi import build_asgi_app

#: The fixed tool names (the stable public contract: seven primitives
#: plus the four intent-level cmdb_* tools).
STABLE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "get_models",
        "get_instances",
        "create_instance",
        "update_instance",
        "delete_instance",
        "search_instances",
        "get_relations",
        "cmdb_preflight_write",
        "cmdb_create_card",
        "cmdb_update_card",
        "cmdb_link_relations",
    }
)


def _clean_env() -> dict[str, str]:
    """A child-process environment free of adapter settings."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MCP_", "VEOPS_"))}
    return env


async def _asgi_request(app: Any, method: str, url: str, **kwargs: Any) -> httpx.Response:
    """Issue one request against the ASGI app through httpx.

    ``url`` is a path such as ``"/healthz"``; it is resolved against the
    ASGI transport's base so httpx can handle the request's cookie
    extraction (which requires an absolute URL).
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.request(method, url, **kwargs)


async def _drive_mcp_session(
    app: Any,
    headers: dict[str, str] | None = None,
    *,
    tools_call: bool = False,
) -> dict[str, Any]:
    """Run the MCP client against ``POST /mcp`` in-process.

    Returns a summary dict with the protocol version, the tool names
    from ``tools/list`` and (optionally) the ``tools/call`` result.
    """
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    summary: dict[str, Any] = {"tools": None, "call": None}

    # The SDK client accepts a pre-configured httpx.AsyncClient; we pass
    # an ASGI-transport client so the whole thing runs in-process, plus
    # our authorization headers when the app requires a token.
    http_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers=headers or {},
    )

    async with streamable_http_client("http://testserver/mcp", http_client=http_client) as (
        read_stream,
        write_stream,
        _get_session_id,
    ), ClientSession(read_stream, write_stream) as session:
        init = await session.initialize()
        summary["protocol_version"] = init.protocolVersion
        summary["server_name"] = init.serverInfo.name
        tools = await session.list_tools()
        summary["tools"] = [t.name for t in tools.tools]
        if tools_call:
            result = await session.call_tool("get_models", {})
            summary["call"] = result
    return summary


def _run_asgi(coro: Any, settings: Settings | None = None) -> Any:
    """Drive an ASGI-based coroutine on a fresh event loop.

    The Starlette lifespan context and the MCP client session must both
    live on the same running loop; a fresh loop keeps the test
    hermetic (no interference with pytest's own loop, if any).

    ``settings`` controls which configuration the ASGI app is built with
    (defaults to a loopback mock adapter with no token).
    """

    effective = settings or Settings(adapter="mock")

    async def _runner() -> Any:
        import contextlib

        app = build_asgi_app(effective, create_app(effective))
        async with contextlib.AsyncExitStack() as stack:
            await stack.enter_async_context(app.router.lifespan_context(app))
            return await coro(app)

    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(_runner())
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def test_mcp_full_handshake_and_stable_tools() -> None:
    """initialize → tools/list → tools/call over POST /mcp."""
    summary = _run_asgi(
        lambda app: _drive_mcp_session(app, tools_call=True)
    )
    assert summary["protocol_version"]
    assert set(summary["tools"]) == STABLE_TOOL_NAMES, f"got {summary['tools']}"
    result = summary["call"]
    assert result.isError is False, f"call failed: {result.content}"


def test_loopback_without_token_allows_mcp() -> None:
    """A loopback bind without a configured token must allow /mcp."""
    summary = _run_asgi(lambda app: _drive_mcp_session(app))
    assert set(summary["tools"]) == STABLE_TOOL_NAMES


def test_non_loopback_requires_token() -> None:
    """0.0.0.0 + token: missing or wrong Authorization → 401."""
    settings = Settings(adapter="mock", bind_host="0.0.0.0", auth_token="s3cr3t-tok3n")

    async def _scenario(_app: Any) -> dict[str, Any]:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
        }
        base_headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        no_auth = await _asgi_request(_app, "POST", "/mcp", json=body, headers=base_headers)
        wrong = await _asgi_request(
            _app,
            "POST",
            "/mcp",
            json=body,
            headers={**base_headers, "Authorization": "Bearer wrong"},
        )
        ok_headers = {**base_headers, "Authorization": "Bearer s3cr3t-tok3n"}
        ok = await _asgi_request(_app, "POST", "/mcp", json=body, headers=ok_headers)
        return {
            "no_auth": (no_auth.status_code, no_auth.json()),
            "wrong": (wrong.status_code, wrong.json()),
            "ok": ok.status_code,
        }

    result = _run_asgi(_scenario, settings)
    assert result["no_auth"][0] == 401
    assert result["wrong"][0] == 401
    # The 401 bodies must never contain the configured token.
    for _, body in (result["no_auth"], result["wrong"]):
        assert "s3cr3t-tok3n" not in str(body)
    assert result["ok"] == 200


def test_non_loopback_token_allows_full_session() -> None:
    """With the correct token, the whole MCP handshake must succeed."""

    async def _scenario(_app: Any) -> dict[str, Any]:
        # The ASGI app built for this test binds 0.0.0.0 with a token;
        # the client must present it.
        return await _drive_mcp_session(_app, headers={"Authorization": "Bearer s3cr3t-tok3n"})

    # Rebuild the app with the auth-enabled settings inside the same
    # runner loop (the app must be created on the loop that serves it).
    import contextlib

    settings = Settings(adapter="mock", bind_host="0.0.0.0", auth_token="s3cr3t-tok3n")

    async def _runner() -> Any:
        app = build_asgi_app(settings, create_app(settings))
        async with contextlib.AsyncExitStack() as stack:
            await stack.enter_async_context(app.router.lifespan_context(app))
            return await _scenario(app)

    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        summary = loop.run_until_complete(_runner())
    finally:
        loop.close()
        asyncio.set_event_loop(None)
    assert set(summary["tools"]) == STABLE_TOOL_NAMES


def test_healthz_always_200() -> None:
    async def _scenario(_app: Any) -> dict[str, Any]:
        resp = await _asgi_request(_app, "GET", "/healthz")
        return {"status": resp.status_code, "body": resp.json()}

    result = _run_asgi(_scenario)
    assert result["status"] == 200
    assert result["body"]["status"] == "ok"


def test_readyz_200_with_valid_config() -> None:
    async def _scenario(_app: Any) -> dict[str, Any]:
        resp = await _asgi_request(_app, "GET", "/readyz")
        return {"status": resp.status_code, "body": resp.json()}

    result = _run_asgi(_scenario)
    assert result["status"] == 200
    assert result["body"]["status"] == "ok"


def test_readyz_503_on_invalid_config() -> None:
    """A non-loopback bind without a token must report 503 unavailable."""

    bad_settings = Settings(adapter="mock", bind_host="0.0.0.0")

    async def _runner() -> dict[str, Any]:
        import contextlib

        app = build_asgi_app(bad_settings, create_app(Settings(adapter="mock")))
        async with contextlib.AsyncExitStack() as stack:
            await stack.enter_async_context(app.router.lifespan_context(app))
            resp = await _asgi_request(app, "GET", "/readyz")
            return {"status": resp.status_code, "body": resp.json()}

    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(_runner())
    finally:
        loop.close()
        asyncio.set_event_loop(None)
    assert result["status"] == 503
    assert result["body"]["status"] == "unavailable"
    assert len(result["body"]["reasons"]) >= 1
    # No secret may appear in the readiness body.
    assert "s3cr3t" not in str(result["body"])


def test_readyz_redacts_prober_reason() -> None:
    """A prober that returns a reason containing the token must be redacted."""

    def _failing_prober(_upstream: UpstreamSettings | None) -> dict[str, Any]:
        return {
            "status": "unavailable",
            "reason": "upstream refused (token: s3cr3t-tok3n)",
        }

    settings = Settings(adapter="mock", auth_token="s3cr3t-tok3n")
    upstream = UpstreamSettings(base_url="https://veops.example", token="s3cr3t-tok3n")

    async def _runner() -> dict[str, Any]:
        import contextlib

        app = build_asgi_app(
            settings, create_app(settings), prober=_failing_prober, upstream=upstream
        )
        async with contextlib.AsyncExitStack() as stack:
            await stack.enter_async_context(app.router.lifespan_context(app))
            resp = await _asgi_request(app, "GET", "/readyz")
            return {"status": resp.status_code, "body": resp.json()}

    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(_runner())
    finally:
        loop.close()
        asyncio.set_event_loop(None)
    assert result["status"] == 503
    body_str = str(result["body"])
    assert "s3cr3t-tok3n" not in body_str, f"secret leaked: {body_str}"
    assert "unavailable" in body_str


def test_readyz_redacts_upstream_token_in_reason() -> None:
    """A prober reason echoing the *upstream* CMDB token must be scrubbed.

    The readiness endpoint only knew about the inbound MCP token; a
    probe that echoes an ``Authorization: Bearer <upstream token>``
    header from the CMDB must not leak it into the response.
    """

    def _failing_prober(_upstream: UpstreamSettings | None) -> dict[str, Any]:
        return {
            "status": "unavailable",
            "reason": "upstream refused (Authorization: Bearer upst-stream-tok3n-9999)",
        }

    settings = Settings(adapter="mock")
    upstream = UpstreamSettings(base_url="https://veops.example", token="upst-stream-tok3n-9999")

    async def _runner() -> dict[str, Any]:
        import contextlib

        app = build_asgi_app(
            settings, create_app(settings), prober=_failing_prober, upstream=upstream
        )
        async with contextlib.AsyncExitStack() as stack:
            await stack.enter_async_context(app.router.lifespan_context(app))
            resp = await _asgi_request(app, "GET", "/readyz")
            return {"status": resp.status_code, "body": resp.json()}

    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(_runner())
    finally:
        loop.close()
        asyncio.set_event_loop(None)
    assert result["status"] == 503
    body_str = str(result["body"])
    assert "upst-stream-tok3n-9999" not in body_str, f"upstream secret leaked: {body_str}"
    assert "unavailable" in body_str


def test_unknown_path_returns_json_404() -> None:
    async def _scenario(_app: Any) -> dict[str, Any]:
        resp = await _asgi_request(_app, "GET", "/nope")
        return {"status": resp.status_code, "body": resp.json()}

    result = _run_asgi(_scenario)
    assert result["status"] == 404
    assert result["body"]["status"] == "not_found"
    assert result["body"]["path"] == "/nope"


class TestRealProcess:
    """End-to-end against a real uvicorn process on a localhost port."""

    PORT = 18331

    @staticmethod
    def _spawn(env_extra: dict[str, str]) -> subprocess.Popen[str]:
        env = _clean_env()
        env.update(env_extra)
        return subprocess.Popen(
            [
                sys.executable,
                "-m",
                "cmdb_mcp_adapter",
                "http",
                "--host",
                "127.0.0.1",
                "--port",
                str(TestRealProcess.PORT),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )

    @staticmethod
    def _wait_healthy(timeout: float = 15.0) -> bool:
        import urllib.request

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{TestRealProcess.PORT}/healthz", timeout=1
                ) as resp:
                    if resp.status == 200:
                        return True
            except Exception:
                time.sleep(0.2)
        return False

    def test_sigint_graceful_shutdown_and_stdout_clean(self) -> None:
        """SIGINT must terminate the server cleanly; stdout stays empty."""
        proc = self._spawn({"MCP_ADAPTER": "mock"})
        try:
            assert self._wait_healthy(), "server did not become healthy in time"
            proc.send_signal(signal.SIGINT)
            out, err = proc.communicate(timeout=15)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            code = proc.returncode
            pytest.fail(f"server did not exit after SIGINT; last stderr: {err[-500:]}")
        # Non-zero exit is the documented behavior for a signal-driven
        # shutdown (the process was interrupted, not cleanly terminated
        # by the normal exit path).
        assert code not in (0, None), f"expected non-zero exit, got {code}"
        assert out == "", f"stdout must be empty on signal exit: {out!r}"
        # The structured log lines were flushed before exit.
        assert "Application shutdown complete" in err or "shutting down" in err or "shutdown" in err
