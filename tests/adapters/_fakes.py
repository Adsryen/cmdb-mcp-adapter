"""Shared fakes for the veops adapter tests: a signature-verifying
``httpx.MockTransport`` that emulates the *real* OneOps/VeOps 2.6.2
contract (``/api/v0.1`` routes, ``_key``/``_secret`` query auth) and
helpers to build a client / adapter against it.

No real network is ever touched: the transport answers canned
responses keyed by ``(METHOD, path)`` or a per-call queue, and records
every request it is asked to make so tests can assert on the params
and call counts.

**Signature verification is enforced in the stub itself**, using the
*same* :func:`~cmdb_mcp_adapter.adapters.veops.signing.sign` function
the production client calls (imported, never copied): a request that
arrives without a valid ``_key``/``_secret`` pair is answered with
``401 {"message": "401 Unauthorized: unauthorized"}`` — exactly what
the live deployment does.  This keeps any regression in the signing
algorithm from passing silently.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

from cmdb_mcp_adapter.adapters.veops import signing
from cmdb_mcp_adapter.adapters.veops.adapter import VeopsAdapter
from cmdb_mcp_adapter.adapters.veops.client import VeopsClient
from cmdb_mcp_adapter.adapters.veops.config import VeopsConfig
from cmdb_mcp_adapter.config import UpstreamSettings

#: Deterministic fake credentials.  Long enough to be token-shaped so
#: the redaction path is exercised; declared as secrets wherever they
#: cross the log boundary.
FAKE_KEY = "veops-test-key-00000000000000000000"
FAKE_SECRET = "veops-test-secret-000000000000000000"


def make_config(
    *,
    base_url: str = "http://veops.local",
    key: str = FAKE_KEY,
    secret: str = FAKE_SECRET,
    timeout_seconds: float = 2.0,
    max_retries: int = 2,
) -> VeopsConfig:
    return VeopsConfig(
        base_url=base_url,
        key=key,
        secret=secret,
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
    )


def _verify_signature(request: httpx.Request) -> str | None:
    """Return ``None`` when the request is correctly signed for
    (FAKE_KEY, FAKE_SECRET); otherwise the 401 body (the live
    deployment's exact error shape).

    Mirrors the upstream ``auth_required`` wrapper: when a JSON body is
    present the credentials and the signed value set live *in the body*;
    otherwise they live in the query string.  A bad or missing signature
    is answered with the exact 401 body the deployment sends.
    """
    import json as _json

    body: dict[str, Any] | None = None
    if request.method != "GET" and request.content:
        try:
            parsed = _json.loads(request.content)
            if isinstance(parsed, dict):
                body = parsed
        except ValueError:
            body = None

    if body is not None:
        key = body.get("_key")
        secret_param = body.get("_secret")
        values = {
            k: v
            for k, v in body.items()
            if k not in ("_key", "_secret") and v is not None
            and not isinstance(v, (dict, list))
        }
    else:
        query = dict(request.url.params)
        key = query.get("_key")
        secret_param = query.get("_secret")
        values = {
            k: v
            for k, v in query.items()
            if k not in ("_key", "_secret") and v is not None
            and not isinstance(v, (dict, list))
        }

    if not key or not secret_param or key != FAKE_KEY:
        return '{"message": "401 Unauthorized: unauthorized"}'
    expected = signing.sign(request.url.path, FAKE_SECRET, values)
    if secret_param != expected:
        return '{"message": "401 Unauthorized: unauthorized"}'
    return None


class _RecordingTransport(httpx.MockTransport):
    """An ``httpx.MockTransport`` that also records every request and
    enforces the veops signing contract on the query string.

    Handlers are looked up by ``(METHOD, path)`` where ``path`` is the
    full request path (including the ``/api/v0.1`` prefix).  A handler
    may be:

    * a ``dict`` payload -> 200 with a JSON body,
    * an ``(int_status, dict_or_str)`` tuple -> that status,
    * a callable ``(request) -> httpx.Response`` for full control,
    * ``None`` -> fall through to the ``default`` handler.

    ``scripts`` is a per-``(METHOD, path)`` FIFO queue of responses to
    serve in order (used to simulate "first two attempts 500, third
    succeeds").  Once a script is exhausted the ``default`` handler
    applies.

    Unless ``require_signature=False``, a request failing signature
    verification is answered 401 *before* any handler is consulted
    (matching the upstream: authentication runs before routing).
    """

    def __init__(
        self,
        handlers: dict[tuple[str, str], Any],
        default: Any,
        *,
        scripts: dict[tuple[str, str], list[Any]] | None = None,
        require_signature: bool = True,
    ) -> None:
        self.handlers = handlers
        self.default = default
        self.scripts = scripts or {}
        self.require_signature = require_signature
        self.requests: list[httpx.Request] = []
        super().__init__(self._dispatch)

    def _resolve(self, request: httpx.Request) -> httpx.Response:
        key = (request.method.upper(), request.url.path)
        queue = self.scripts.get(key)
        item = queue.pop(0) if queue else self.handlers.get(key, self.default)
        if callable(item) and not isinstance(item, dict):
            return item(request)
        if isinstance(item, tuple):
            status, body = item
            return _json_response(status, body)
        if isinstance(item, dict):
            return _json_response(200, item)
        if item is None:
            return httpx.Response(404, json={"error": "not found"})
        return _json_response(200, item)

    def _dispatch(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.require_signature:
            failure = _verify_signature(request)
            if failure is not None:
                return httpx.Response(401, json={"message": "401 Unauthorized: unauthorized"})
        return self._resolve(request)


def _json_response(status: int, body: Any) -> httpx.Response:
    if isinstance(body, str):
        return httpx.Response(status, content=body.encode("utf-8"))
    if status == 204 or body is None:
        return httpx.Response(status)
    return httpx.Response(status, json=body)


def build_client(
    transport: _RecordingTransport,
    *,
    config: VeopsConfig | None = None,
) -> VeopsClient:
    return VeopsClient(config or make_config(), transport=transport)


def build_adapter(
    transport: _RecordingTransport,
    *,
    config: VeopsConfig | None = None,
    max_retries: int = 2,
    sleep: Callable[[float], None] | None = None,
) -> tuple[VeopsAdapter, VeopsClient]:
    """Build an adapter whose retry ``sleep`` is a no-op (tests never
    wait)."""
    import cmdb_mcp_adapter.observability.retry as retry_mod

    if sleep is None:
        sleep = lambda _s: None  # noqa: E731

    retry_mod._sleep = sleep  # type: ignore[attr-defined]
    cfg = config or make_config(max_retries=max_retries)
    client = VeopsClient(cfg, transport=transport)
    adapter = VeopsAdapter(client, max_retries=max_retries)
    return adapter, client


def upstream_settings(
    *,
    base_url: str | None = "http://veops.local",
    key: str | None = FAKE_KEY,
    secret: str | None = FAKE_SECRET,
    token: str | None = None,
) -> UpstreamSettings:
    return UpstreamSettings(base_url=base_url, key=key, secret=secret, token=token)
