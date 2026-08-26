"""Inbound authentication for the (future) HTTP transport.

The MVP auth model is deliberately minimal and fail-closed:

* **Loopback bind** — when the adapter binds to a loopback address
  (127.0.0.1 / ::1 / localhost), the HTTP endpoint may accept requests
  without a token (the local trust boundary is the host).
* **Non-loopback bind** — requires ``MCP_AUTH_TOKEN`` to be configured
  (enforced at startup by :func:`cmdb_mcp_adapter.config.validate_settings`)
  and every request must carry a matching ``Authorization: Bearer <token>``
  header.  Tokens are compared in constant time via
  :func:`hmac.compare_digest`.
* **No token configured** — the endpoint rejects *all* requests.  There
  is no implicit open mode for a network bind.

This module ships the pure decision function :func:`authorize` plus a
middleware factory :func:`make_auth_middleware` that the remote-http task
wraps into an ASGI/uvicorn middleware.  Nothing here starts a server or
touches the network, so it is fully unit-testable.
"""

from __future__ import annotations

import hmac
from collections.abc import Callable
from typing import Any

from cmdb_mcp_adapter.config import Settings

#: The header name (lower-cased) carrying the bearer token.
AUTHORIZATION_HEADER = "authorization"

#: The ``Authorization`` scheme this adapter accepts.
BEARER_SCHEME = "Bearer"


def _extract_bearer(headers: dict[str, Any]) -> str | None:
    """Pull the bearer token out of a header mapping, case-insensitively.

    Accepts ``"Bearer <token>"`` (scheme case-insensitive).  Returns
    ``None`` when the header is absent, malformed, or empty.
    """
    value: Any = None
    for key, val in headers.items():
        if str(key).lower() == AUTHORIZATION_HEADER:
            value = val
            break
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    parts = value.split(None, 1)
    if len(parts) != 2:
        return None
    scheme, token = parts
    if scheme.lower() != BEARER_SCHEME.lower():
        return None
    token = token.strip()
    return token or None


def authorize(settings: Settings, headers: dict[str, Any]) -> str | None:
    """Decide whether an inbound request is authorized.

    Returns ``None`` when authorized, otherwise a human-readable error
    message suitable for a 401 response body.  The decision is a pure
    function of the (already startup-validated) settings and the request
    headers; no I/O, no side effects.

    Rules:

    * loopback bind → authorized without a token;
    * non-loopback bind → a ``Bearer`` token equal to
      ``settings.auth_token`` (constant-time compare) is required;
      anything else is rejected.
    """
    if settings.is_loopback():
        return None
    if settings.auth_token is None:
        # Should be impossible after validate_settings, but fail closed
        # rather than open if someone constructed settings by hand.
        return "authentication required (no token configured)"
    provided = _extract_bearer(headers)
    if provided is None:
        return "missing bearer token"
    if not hmac.compare_digest(provided.encode("utf-8"), settings.auth_token.encode("utf-8")):
        return "invalid bearer token"
    return None


def make_auth_middleware(settings: Settings) -> Callable[[dict[str, Any]], str | None]:
    """Build an auth middleware callable for the HTTP transport.

    Returns a function ``headers -> error | None`` — the same contract as
    :func:`authorize` — captured against the settings it was built from.
    The remote-http task wraps this into an ASGI/uvicorn middleware (or a
    FastMCP dependency) without any further auth logic: ``None`` means
    ``allow``, a string means ``reject with this message``.

    Keeping the factory here (rather than in the transport) means the
    auth policy has exactly one owner and the transport stays thin.
    """
    return lambda headers: authorize(settings, headers)
