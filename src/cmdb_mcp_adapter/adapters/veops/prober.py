"""VeOps reachability prober (wired to the ASGI ``/readyz`` endpoint).

The prober issues an *unauthenticated* ``GET`` to a known, cheap,
read-only endpoint (``/api/v0.1/ci_types``) — deliberately without
``_key``/``_secret``: a 401 "unauthenticated" answer is itself proof
that the upstream is up.  This keeps the readiness path free of
credential handling (nothing to redact, nothing that can fail on a
rotated key) and works even before the adapter's first request.

Decision table (verified against the 2.6.2 deployment, 2026-08):

* **2xx / 3xx** response -> ``ok``.
* **401 / 403** (unauthenticated / forbidden) -> ``ok`` (the server
  answered; it is only refusing an anonymous probe).
* **Connect / timeout / network errors** and **5xx** responses ->
  ``unavailable`` with a reason.

The probe is read-only (a single ``GET``) and bounded by the configured
timeout, so it is safe to run on every readiness check.  The ``reason``
string is scrubbed by the shared redaction filter before it reaches the
readiness endpoint body.
"""

from __future__ import annotations

from typing import Any

from cmdb_mcp_adapter.adapters.veops.client import VeopsClient
from cmdb_mcp_adapter.adapters.veops.config import (
    PROBE_PATH,
    build_veops_config,
)
from cmdb_mcp_adapter.config import UpstreamSettings

_OK_STATUSES: frozenset[int] = frozenset(range(200, 400)) | frozenset({401, 403})


def make_veops_prober(client: VeopsClient) -> Any:
    """Build an :class:`UpstreamProber` around an existing veops client.

    The prober reuses the client's transport (so tests can inject a
    mock) but issues its own lightweight, unauthenticated ``GET`` to
    the probe endpoint.  It ignores the ``UpstreamSettings`` argument
    the readiness layer passes (the client already carries the base URL;
    the probe must not attach credentials).
    """

    def _probe(_upstream: UpstreamSettings | None) -> dict[str, Any]:
        try:
            response = client.request_probe(PROBE_PATH)
        except Exception as exc:  # noqa: BLE001 - probe must never raise
            return {"status": "unavailable", "reason": f"probe failed: {type(exc).__name__}"}
        if response.status_code in _OK_STATUSES:
            return {"status": "ok"}
        return {
            "status": "unavailable",
            "reason": f"upstream returned HTTP {response.status_code}",
        }

    return _probe


def build_prober_from_upstream(upstream: UpstreamSettings) -> Any:
    """Convenience: build a prober directly from upstream settings.

    Fails closed (raises :class:`ConfigurationError`) if the veops
    configuration is incomplete, matching the adapter's own startup
    policy.  This is what the HTTP launcher uses; it needs a timeout, so
    it uses a fixed 5-second probe timeout (the launcher passes the
    inbound timeout through :func:`build_veops_config`).
    """
    config = build_veops_config(upstream, timeout_seconds=5.0, max_retries=0)
    client = VeopsClient(config)
    return make_veops_prober(client)


__all__ = [
    "build_prober_from_upstream",
    "make_veops_prober",
]
