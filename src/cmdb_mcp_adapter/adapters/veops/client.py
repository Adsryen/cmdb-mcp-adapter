"""HTTP client for the VeOps CMDB (OneOps/VeOps 2.6.2).

:class:`VeopsClient` wraps a synchronous :class:`httpx.Client` and is the
*only* place that knows how to talk to the VeOps API.  It is deliberately
transport-injectable: the ``transport`` argument lets tests substitute an
``httpx.MockTransport`` (or any ``httpx.BaseTransport``) so the entire
adapter can be exercised without network access or real credentials.

Authentication (verified against a live 2.6.2 deployment, 2026-08):
every request — ``GET``/``POST``/``PUT``/``DELETE`` — carries two *query*
parameters:

* ``_key`` — the VeOps user key (an identity);
* ``_secret`` — ``sha1(request.path + secret + <sorted scalar values>)``,
  where the value suffix concatenates ``str(v)`` of every scalar request
  parameter (query and, for write calls, scalar JSON-body values),
  excluding ``_key``/``_secret``, sorted by parameter name.

See :mod:`.signing` for the pure functions.  There is no
``Authorization`` header.  The user *secret* is registered as a known
secret with the shared redaction filter and is never echoed in logs or
error messages; the key is likewise redacted as a known secret.

Responsibilities kept here (per the design boundary):

* URL construction (``base_url`` + :data:`API_PREFIX` + the per-call path);
* per-request signing (see :func:`~cmdb_mcp_adapter.adapters.veops.signing.sign);
* the per-request timeout (consumed from the inbound settings);
* mapping upstream HTTP status codes onto stable core error categories;
* structured request/response logging with the redaction filter applied.

Error mapping (see ``docs/veops-adapter.md`` for the full table).  An
*invalid* signature — like a missing ``_key`` — is answered by the
upstream with **401** ``unauthorized``, so a bad credential pair surfaces
as :class:`AuthenticationError` (never as 200-with-a-flag):

* timeout / connection error  -> :class:`TimeoutError` (core)
* 401 (incl. bad signature)   -> :class:`AuthenticationError`
* 403                        -> :class:`AuthorizationError`
* 404                        -> :class:`NotFoundError`
* 429                        -> :class:`RateLimitError`
* 5xx                        -> :class:`UpstreamServerError`
* any other 4xx              -> :class:`ValidationError`

The client-facing ``message`` is always a clean, category-appropriate
string that never contains the key/secret or the raw body.  The raw
upstream body (truncated) is carried in ``upstream_detail`` for protected
logs only; the MCP tool layer never emits ``upstream_detail``.
"""

from __future__ import annotations

import logging
from typing import Any, cast

import httpx

from cmdb_mcp_adapter.adapters.veops import signing
from cmdb_mcp_adapter.adapters.veops.config import API_PREFIX, VeopsConfig
from cmdb_mcp_adapter.core.errors import (
    AuthenticationError,
    AuthorizationError,
    NotFoundError,
    RateLimitError,
    TimeoutError,
    UpstreamServerError,
    ValidationError,
)
from cmdb_mcp_adapter.observability import redaction

_logger = logging.getLogger("cmdb_mcp_adapter.adapters.veops")

#: The maximum number of raw-body bytes retained in ``upstream_detail`` for
#: diagnostics.  Bounded so a hostile / huge error payload cannot balloon
#: log lines; the body is redacted before it is ever logged.
_MAX_DETAIL_BYTES = 512


class VeopsClient:
    """A synchronous, transport-injectable HTTP client for VeOps."""

    def __init__(
        self,
        config: VeopsConfig,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._config = config
        # Both the user *secret* and the user *key* are registered as
        # known secrets so the shared redaction filter scrubs them from
        # anything we log, even if either were ever embedded in an
        # upstream payload or echoed in a URL.
        self._redaction_secrets: frozenset[str] = frozenset({config.key, config.secret})
        self._client = httpx.Client(
            base_url=config.base_url,
            headers={"Accept": "application/json"},
            timeout=httpx.Timeout(config.timeout_seconds),
            transport=transport,
        )

    @property
    def base_url(self) -> str:
        return self._config.base_url

    @property
    def config(self) -> VeopsConfig:
        return self._config

    def close(self) -> None:
        self._client.close()

    def request_probe(self, path: str) -> httpx.Response:
        """A raw reachability probe: a ``GET`` that never maps status codes.

        The probe is *unauthenticated* on purpose: it is the readiness
        check, and a 401 ("unauthenticated") answer is exactly what proves
        the upstream is up.  Returns the raw :class:`httpx.Response` (any
        status) so the prober can decide reachability ({200, 401, 403} =
        up; 5xx / network error = down).  Transport-level failures
        (connect / timeout / network) raise the original ``httpx``
        exception, which the prober catches.
        """
        url = f"{API_PREFIX}{path}"
        return self._client.request("GET", url)

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Issue one signed request; the body must be a JSON *object*."""
        return cast(
            "dict[str, Any]",
            self._request(method, path, params=params, json_body=json_body, expect=dict),
        )

    def request_array(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> list[Any]:
        """Issue one signed ``GET`` whose body is a **bare JSON array**.

        ``GET /relation_types`` answers a top-level array (verified
        2026-09-22: 8 entries of ``{id, name, deleted, created_at,
        updated_at, deleted_at}``) instead of the usual list wrapper.
        The array contract is deliberately **opt-in**: :meth:`request`
        keeps rejecting a non-object body, so no other endpoint can
        quietly start returning an array without a loud error.
        """
        return cast(
            "list[Any]",
            self._request("GET", path, params=params, json_body=None, expect=list),
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None,
        json_body: dict[str, Any] | None,
        expect: type[dict[str, Any]] | type[list[Any]],
    ) -> Any:
        """Shared signed-request body (see :meth:`request`).

        Returns the parsed JSON body, which must be of the ``expect``ed
        top-level type: a non-matching shape is an upstream contract
        violation and raises (it is never coerced or ignored).

        ``path`` is relative to :data:`API_PREFIX` (it should start with a
        ``/``).  ``params`` are the request's query parameters and
        ``json_body`` is the JSON body for write calls; ``_key`` and
        ``_secret`` are added here (never supplied by callers).

        Where the credentials travel depends on the upstream's
        ``auth_required`` wrapper: for **read** calls (no JSON body) it
        signs ``request.values`` (the query string) and the credentials
        ride as query parameters; for **write** calls (a JSON body is
        present) it does ``request.values = request.json``, so it looks
        for the credentials *inside the body* and signs over the body's
        scalar values — hence the credentials go in the JSON body and the
        signature covers the body scalars.  See :mod:`.signing`.

        Returns the decoded JSON object; a ``204`` (no content)
        response yields an empty dict.

        Raises:
            CoreError: with the stable category described in the module
                docstring, on any non-2xx response or transport failure.
        """
        url = f"{API_PREFIX}{path}"
        query = {k: v for k, v in (params or {}).items() if v is not None}
        # The upstream's ``auth_required`` rewrites ``request.values`` to the
        # parsed JSON body when one is present, so the place it looks for
        # ``_key``/``_secret`` — and the value set it signs — differs by
        # whether a body is sent:
        #   * no body (reads):   credentials in the query, signed over the
        #                        query scalar values;
        #   * JSON body (writes):credentials inside the body, signed over the
        #                        body's scalar values.
        if json_body is not None:
            sign_params = {k: v for k, v in json_body.items() if v is not None}
            sent_body = {k: v for k, v in json_body.items() if v is not None}
            sent_body[signing.KEY_PARAM] = self._config.key
            sent_body[signing.SECRET_PARAM] = signing.sign(
                url, self._config.secret, sign_params
            )
            params_to_send: dict[str, Any] | None = None
            body_to_send = sent_body
        else:
            body_to_send = None
            params_to_send = signing.signed_params(
                url, self._config.key, self._config.secret, query
            )

        _log_request(method, url, query)

        try:
            response = self._client.request(
                method, url, params=params_to_send, json=body_to_send
            )
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
            _logger.info(
                "veops.request.timeout",
                extra={"event": "veops.request.timeout", "method": method, "path": url},
            )
            raise TimeoutError(
                f"veops request {method} {path} timed out or the connection "
                "failed",
                upstream_detail=type(exc).__name__,
            ) from exc

        _log_response(method, url, response.status_code)
        if 200 <= response.status_code < 300:
            if response.status_code == 204 or not response.content:
                return {} if expect is dict else []
            try:
                data = response.json()
            except ValueError as exc:
                raise ValidationError(
                    "veops returned a non-JSON success body",
                    upstream_detail=self._detail(response),
                ) from exc
            if not isinstance(data, expect):
                raise ValidationError(
                    f"veops returned an unexpected JSON body: a "
                    f"{expect.__name__} was required, got "
                    f"{type(data).__name__}",
                    upstream_detail=self._detail(response),
                )
            return data

        self._raise_for_status(method, path, response)
        # _raise_for_status always raises; this return keeps mypy from
        # treating the tail of the function as reachable-with-None.
        return {} if expect is dict else []  # pragma: no cover - unreachable

    def _detail(self, response: httpx.Response) -> str:
        """A bounded, redacted snippet of the upstream error body.

        ``upstream_detail`` is retained for *protected* logs, so the raw
        snippet is run through the shared redaction rules before it is
        stored: a hostile upstream body that echoes the adapter's
        ``_key``/``_secret`` (or any sensitive key / token-shaped value)
        cannot survive into ``CoreError.upstream_detail``.
        """
        body = response.text or ""
        return str(redaction.redact(body[:_MAX_DETAIL_BYTES], self._redaction_secrets))


    def _raise_for_status(self, method: str, path: str, response: httpx.Response) -> None:
        status = response.status_code
        detail = self._detail(response)
        if status == 401:
            # Verified: the upstream answers *both* "no _key" and
            # "bad signature" with 401 {"message": "401 Unauthorized:
            # unauthorized"}.  So an invalid credential pair is not a
            # validation problem — it is an authentication failure.
            raise AuthenticationError(
                "veops rejected the adapter credentials (HTTP 401); check "
                "VEOPS_KEY / VEOPS_SECRET",
                upstream_detail=detail,
            )
        if status == 403:
            raise AuthorizationError(
                "veops denied access to this resource (HTTP 403)",
                upstream_detail=detail,
            )
        if status == 404:
            raise NotFoundError(
                f"veops has no resource at {path} (HTTP 404)",
                upstream_detail=detail,
            )
        if status == 429:
            raise RateLimitError(
                "veops is rate-limiting the adapter (HTTP 429)",
                upstream_detail=detail,
            )
        if 500 <= status <= 599:
            raise UpstreamServerError(
                f"veops returned a server error (HTTP {status})",
                upstream_detail=detail,
            )
        # Other 4xx: treat as a validation problem with the request payload.
        raise ValidationError(
            f"veops rejected the request (HTTP {status})",
            upstream_detail=detail,
        )




def _log_request(method: str, url: str, params: dict[str, Any] | None) -> None:
    # The ``_key``/``_secret`` query values are deliberately never logged
    # (the signed values are assembled after this line and both are
    # registered known secrets for the redaction filter).  ``params``
    # (query filters) is logged so operations can be correlated.
    _logger.info(
        "veops.request",
        extra={
            "event": "veops.request",
            "method": method,
            "path": url,
            "params": params,
        },
    )


def _log_response(method: str, url: str, status: int) -> None:
    level = logging.WARNING if status >= 400 else logging.INFO
    _logger.log(
        level,
        "veops.response",
        extra={
            "event": "veops.response",
            "method": method,
            "path": url,
            "status": status,
        },
    )
