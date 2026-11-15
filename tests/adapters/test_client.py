"""Unit tests for :class:`VeopsClient`: signature assembly on the query
string, status mapping (including the verified 401-for-bad-signature
behavior), timeout handling, and the guarantee that the key/secret
never leaks into client-facing error messages."""

from __future__ import annotations

import json

import httpx
import pytest

from cmdb_mcp_adapter.adapters.veops import signing
from cmdb_mcp_adapter.core.errors import (
    AuthenticationError,
    AuthorizationError,
    ErrorCategory,
    NotFoundError,
    RateLimitError,
    TimeoutError,
    UpstreamServerError,
    ValidationError,
)
from tests.adapters._fakes import (
    FAKE_KEY,
    FAKE_SECRET,
    _RecordingTransport,
    build_client,
    make_config,
)

API = "/api/v0.1"


def _expected_secret(path: str, query: dict[str, object]) -> str:
    return signing.sign(path, FAKE_SECRET, {k: v for k, v in query.items()})


class TestSigningOnWire:
    def test_query_carries_key_and_secret(self) -> None:
        transport = _RecordingTransport({}, {})
        client = build_client(transport)
        client.request("GET", "/ci_types")
        (req,) = transport.requests
        query = dict(req.url.params)
        assert query["_key"] == FAKE_KEY
        assert query["_secret"] == _expected_secret("/api/v0.1/ci_types", {})
        # No Authorization header anymore: auth is query-based only.
        assert "Authorization" not in req.headers
        assert req.headers.get("accept") == "application/json"

    def test_params_are_signed(self) -> None:
        transport = _RecordingTransport({}, {})
        client = build_client(transport)
        client.request("GET", "/ci/search", params={"q": "ACC-0166", "page": 1})
        (req,) = transport.requests
        query = dict(req.url.params)
        expected = signing.sign(
            "/api/v0.1/ci/search", FAKE_SECRET, {"q": "ACC-0166", "page": 1}
        )
        assert query["_secret"] == expected
        assert query["q"] == "ACC-0166"

    def test_none_params_are_dropped_before_signing(self) -> None:
        transport = _RecordingTransport({}, {})
        client = build_client(transport)
        client.request("GET", "/ci/search", params={"q": None, "page": 2})
        (req,) = transport.requests
        query = dict(req.url.params)
        assert "q" not in query
        expected = signing.sign("/api/v0.1/ci/search", FAKE_SECRET, {"page": 2})
        assert query["_secret"] == expected

    def test_json_body_carrying_credentials_is_signed(self) -> None:
        # For a JSON-body write the upstream's auth_required sets
        # request.values = request.json, so it reads _key/_secret from the
        # body and signs over the body's scalar values.  The client must
        # put the credentials in the body and sign over the body scalars.
        transport = _RecordingTransport({}, {})
        client = build_client(transport)
        client.request("POST", "/ci", json_body={"ci_type": 7, "credenti_key_id": "K1"})
        (req,) = transport.requests
        import json as _json
        body = _json.loads(req.content)
        # Credentials travel in the body (not the query string).
        assert body["_key"] == FAKE_KEY
        assert "_key" not in dict(req.url.params)
        # The body signature covers the body scalars (minus the creds).
        expected = signing.sign(
            "/api/v0.1/ci", FAKE_SECRET, {"ci_type": 7, "credenti_key_id": "K1"}
        )
        assert body["_secret"] == expected
        # A wrong (query-only) signature must NOT be what was produced.
        assert body["_secret"] != signing.sign("/api/v0.1/ci", FAKE_SECRET, {})

    def test_url_is_base_plus_api_prefix(self) -> None:
        transport = _RecordingTransport({}, {})
        config = make_config(base_url="http://veops.example/")
        client = build_client(transport, config=config)
        client.request("GET", "/ci_types")
        (req,) = transport.requests
        assert req.url.path == f"{API}/ci_types"

    def test_timeout_is_consume_from_config(self) -> None:
        config = make_config(timeout_seconds=7.5)
        client = build_client(_RecordingTransport({}, {}), config=config)
        # The httpx.Client's timeout should reflect the config.
        assert client._client.timeout.connect == pytest.approx(7.5)


class TestStatusMapping:
    @pytest.mark.parametrize(
        ("status", "expected_cls", "expected_category"),
        [
            (401, AuthenticationError, ErrorCategory.AUTHENTICATION),
            (403, AuthorizationError, ErrorCategory.AUTHORIZATION),
            (404, NotFoundError, ErrorCategory.NOT_FOUND),
            (429, RateLimitError, ErrorCategory.RATE_LIMIT),
            (400, ValidationError, ErrorCategory.VALIDATION),
            (500, UpstreamServerError, ErrorCategory.UPSTREAM_SERVER),
            (503, UpstreamServerError, ErrorCategory.UPSTREAM_SERVER),
        ],
    )
    def test_error_status_maps_to_category(
        self, status: int, expected_cls: type, expected_category: ErrorCategory
    ) -> None:
        transport = _RecordingTransport({}, (status, {"error": "boom"}))
        client = build_client(transport)
        with pytest.raises(expected_cls) as exc:
            client.request("GET", "/ci_types")
        assert exc.value.category == expected_category

    def test_success_returns_parsed_json(self) -> None:
        payload = {"ci_types": [{"name": "host", "id": 1}]}
        transport = _RecordingTransport({("GET", f"{API}/ci_types"): payload}, {})
        client = build_client(transport)
        result = client.request("GET", "/ci_types")
        assert result == payload

    def test_204_returns_empty_dict(self) -> None:
        transport = _RecordingTransport(
            {("DELETE", f"{API}/ci/123"): (204, None)}, {}
        )
        client = build_client(transport)
        assert client.request("DELETE", "/ci/123") == {}

    def test_non_json_success_body_is_validation_error(self) -> None:
        transport = _RecordingTransport(
            {("GET", f"{API}/ci_types"): (200, "not-json")}, {}
        )
        client = build_client(transport)
        with pytest.raises(ValidationError) as exc:
            client.request("GET", "/ci_types")
        assert exc.value.category == ErrorCategory.VALIDATION


class TestTimeout:
    def test_timeout_raises_core_timeout(self) -> None:
        def _timeout(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("timed out")

        transport = _RecordingTransport({("GET", f"{API}/ci_types"): _timeout}, {})
        client = build_client(transport)
        with pytest.raises(TimeoutError) as exc:
            client.request("GET", "/ci_types")
        assert exc.value.category == ErrorCategory.TIMEOUT

    def test_connect_error_raises_core_timeout(self) -> None:
        def _err(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route to host")

        transport = _RecordingTransport({("GET", f"{API}/ci_types"): _err}, {})
        client = build_client(transport)
        with pytest.raises(TimeoutError):
            client.request("GET", "/ci_types")


class TestNoCredentialLeak:
    @pytest.mark.parametrize(
        "status", [401, 403, 404, 429, 500, 503, 400]
    )
    def test_error_message_never_contains_credentials(self, status: int) -> None:
        # The upstream body deliberately echoes the credentials; the
        # *client-facing* message must still be clean.
        transport = _RecordingTransport(
            {},
            (
                status,
                {
                    "error": f"denied, key={FAKE_KEY}, secret={FAKE_SECRET}"
                },
            ),
        )
        client = build_client(transport)
        with pytest.raises(Exception) as exc:  # noqa: B017
            client.request("GET", "/ci_types")
        assert FAKE_KEY not in exc.value.message
        assert FAKE_SECRET not in exc.value.message
        assert FAKE_KEY not in str(exc.value)
        assert FAKE_SECRET not in str(exc.value)


class TestProbe:
    def test_probe_sends_no_auth_params(self) -> None:
        # The readiness probe is *unauthenticated* on purpose: a 401
        # answer proves the upstream is up, and the probe must not
        # depend on (or leak) credentials.
        transport = _RecordingTransport(
            {("GET", f"{API}/ci_types"): (401, {"message": "401 Unauthorized: unauthorized"})},
            {},
            require_signature=False,
        )
        client = build_client(transport)
        response = client.request_probe("/ci_types")
        assert response.status_code == 401
        (req,) = transport.requests
        query = dict(req.url.params)
        assert "_key" not in query
        assert "_secret" not in query


class TestBodyNullSemanticsP35:
    """P-35 regression: a caller's explicit JSON ``null`` in a write body
    must be transmitted verbatim and must be counted in the body
    signature (``keep_none=True``).  The 0.2.8 client stripped nulls from
    *both*, which kept auth self-consistent while silently dropping the
    "clear this field" intent (200 OK, nothing changed).

    These tests assert on the recorded wire bytes rather than through
    ``_RecordingTransport``'s signature verification: the shared fake's
    body branch still emulates the old strip-nulls value set, so a
    null-carrying body signed the new (upstream-correct) way would fail
    it.  Verification is disabled and the digest is checked directly
    against :func:`signing.sign` with ``keep_none=True``.
    """

    def test_explicit_null_is_transmitted_not_stripped(self) -> None:
        transport = _RecordingTransport({}, {}, require_signature=False)
        client = build_client(transport)
        client.request(
            "PATCH", "/ci/1", json_body={"ci_type": 7, "note": None}
        )
        (req,) = transport.requests
        body = json.loads(req.content)
        # The null key survives on the wire — that IS the fix.
        assert "note" in body
        assert body["note"] is None
        assert body["ci_type"] == 7

    def test_body_signature_counts_the_null(self) -> None:
        transport = _RecordingTransport({}, {}, require_signature=False)
        client = build_client(transport)
        client.request(
            "PATCH", "/ci/1", json_body={"ci_type": 7, "note": None}
        )
        (req,) = transport.requests
        body = json.loads(req.content)
        assert body["_key"] == FAKE_KEY
        expected = signing.sign(
            "/api/v0.1/ci/1", FAKE_SECRET, {"ci_type": 7, "note": None}, keep_none=True
        )
        assert body["_secret"] == expected
        # Explicitly NOT the 0.2.8 digest (nulls stripped from both the
        # body and the signed set).
        legacy = signing.sign("/api/v0.1/ci/1", FAKE_SECRET, {"ci_type": 7})
        assert body["_secret"] != legacy

    def test_body_without_null_unchanged(self) -> None:
        # No nulls: keep_none=True and the legacy path produce the same
        # digest, so every existing body-signed test stays valid.
        transport = _RecordingTransport({}, {}, require_signature=False)
        client = build_client(transport)
        client.request("POST", "/ci", json_body={"ci_type": 7, "name": "web-01"})
        (req,) = transport.requests
        body = json.loads(req.content)
        assert body["_secret"] == signing.sign(
            "/api/v0.1/ci", FAKE_SECRET, {"ci_type": 7, "name": "web-01"}
        )
