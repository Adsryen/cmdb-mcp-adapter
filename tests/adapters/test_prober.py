"""Tests for the veops reachability prober and its wiring into the ASGI
readiness endpoint.

The probe is *unauthenticated* (no ``_key``/``_secret``): a 401 answer
from ``/api/v0.1/ci_types`` is itself proof the upstream is up, so the
reachable set is {2xx, 3xx, 401, 403}; 5xx / network errors are
unavailable.
"""

from __future__ import annotations

import httpx

from cmdb_mcp_adapter.adapters.veops.config import API_PREFIX, PROBE_PATH
from cmdb_mcp_adapter.adapters.veops.prober import make_veops_prober
from cmdb_mcp_adapter.config import UpstreamSettings
from tests.adapters._fakes import FAKE_KEY, FAKE_SECRET, _RecordingTransport, build_client

PROBE = f"{API_PREFIX}{PROBE_PATH}"


def _prober_for(response: httpx.Response | type[Exception]) -> callable:  # type: ignore[type-arg]
    def handler(_request: httpx.Request) -> httpx.Response:
        if isinstance(response, Exception) or (
            isinstance(response, type) and issubclass(response, Exception)
        ):
            raise response  # type: ignore[misc]
        return response

    transport = _RecordingTransport(
        {("GET", PROBE): handler}, {}, require_signature=False
    )
    client = build_client(transport)
    return make_veops_prober(client)


class TestProberSemantics:
    def test_401_unauthenticated_is_reachable(self) -> None:
        # Verified against the live deployment: an unauthenticated probe
        # gets exactly this answer, and it means "up".
        prober = _prober_for(
            httpx.Response(401, json={"message": "401 Unauthorized: unauthorized"})
        )
        assert prober(None) == {"status": "ok"}

    def test_403_is_reachable(self) -> None:
        prober = _prober_for(httpx.Response(403, json={"error": "forbidden"}))
        assert prober(None) == {"status": "ok"}

    def test_200_is_reachable(self) -> None:
        prober = _prober_for(httpx.Response(200, json={"ci_types": []}))
        assert prober(None) == {"status": "ok"}

    def test_500_is_unavailable(self) -> None:
        prober = _prober_for(httpx.Response(500, json={"error": "boom"}))
        result = prober(None)
        assert result["status"] == "unavailable"
        assert "500" in result["reason"]

    def test_connect_error_is_unavailable(self) -> None:
        prober = _prober_for(httpx.ConnectError("no route"))
        result = prober(None)
        assert result["status"] == "unavailable"
        assert "ConnectError" in result["reason"]
        # The reason must not contain the credentials.
        assert FAKE_KEY not in result["reason"]
        assert FAKE_SECRET not in result["reason"]

    def test_reason_never_contains_credentials(self) -> None:
        prober = _prober_for(httpx.Response(503, json={"error": "down"}))
        result = prober(None)
        assert FAKE_KEY not in str(result)
        assert FAKE_SECRET not in str(result)


class TestProbeRequestShape:
    def test_probe_carries_no_auth_params(self) -> None:
        transport = _RecordingTransport(
            {("GET", PROBE): (401, {"message": "401 Unauthorized: unauthorized"})},
            {},
            require_signature=False,
        )
        client = build_client(transport)
        prober = make_veops_prober(client)
        assert prober(None) == {"status": "ok"}
        (req,) = transport.requests
        assert req.url.path == PROBE
        query = dict(req.url.params)
        assert "_key" not in query
        assert "_secret" not in query


class TestProberWiring:
    def test_run_http_wires_prober_and_upstream(self) -> None:
        """run_http must build the ASGI app with a veops prober and the
        upstream settings.  We spy on ``build_asgi_app`` and stub
        ``_serve_blocking`` so no port is bound."""
        import cmdb_mcp_adapter.server.http as http
        from cmdb_mcp_adapter.config import Settings

        seen: dict[str, object] = {}
        served: dict[str, object] = {}

        def spy(_settings, _mcp_server, *, prober=None, upstream=None):
            seen["prober"] = prober
            seen["upstream"] = upstream
            return object()

        def fake_serve_blocking(settings, asgi_app):
            served["called"] = True
            served["asgi_app"] = asgi_app

        # build_asgi_app is resolved from the asgi module inside run_http,
        # so we must patch it there (not on the http module).
        import cmdb_mcp_adapter.server.asgi as asgi

        real_build = asgi.build_asgi_app
        real_serve = http._serve_blocking
        asgi.build_asgi_app = spy
        http._serve_blocking = fake_serve_blocking
        try:
            settings = Settings(
                adapter="veops",
                bind_host="127.0.0.1",
            )
            import os

            old = {k: v for k, v in os.environ.items()}
            os.environ["VEOPS_BASE_URL"] = "http://127.0.0.1:1"
            os.environ["VEOPS_KEY"] = FAKE_KEY
            os.environ["VEOPS_SECRET"] = FAKE_SECRET
            try:
                http.run_http(settings)
            finally:
                os.environ.clear()
                os.environ.update(old)
        finally:
            asgi.build_asgi_app = real_build
            http._serve_blocking = real_serve

        assert seen.get("prober") is not None, "run_http must pass a prober"
        assert seen.get("upstream") is not None, "run_http must pass upstream"
        assert isinstance(seen["upstream"], UpstreamSettings)
        assert seen["upstream"].base_url == "http://127.0.0.1:1"
        assert served.get("called") is True
