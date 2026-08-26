"""Liveness and readiness probes.

**Liveness** answers "is the process alive?": it performs no work beyond
constructing a constant response, so it must succeed even while the
process is shutting down or a dependency is down.

**Readiness** answers "is the process ready to serve traffic?": it
validates the *inbound* configuration (a non-loopback bind requires an
auth token; a misconfigured log level or upstream base URL fails) and
may optionally run a *cheap, non-destructive* upstream reachability
probe.  The probe is **injectable**: the MVP default is a no-op that
reports ok, and the veops/remote-http tasks supply a real HTTP prober.
The probe must never perform a write or other destructive operation.

These functions are transport-agnostic and return plain dicts so the
HTTP task can serialize them directly (and the stdio task can log them).
Neither probe performs any network I/O by default, so importing this
module has zero side effects.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from cmdb_mcp_adapter.config import (
    Settings,
    UpstreamSettings,
    validate_settings,
    validate_upstream_settings,
)

#: A probe checks upstream reachability without side effects.  It returns
#: a mapping describing the result (e.g. ``{"status": "ok"}`` or
#: ``{"status": "unavailable", "reason": ...}``).  The default is a
#: no-op that always reports ok.
UpstreamProber = Callable[[UpstreamSettings | None], dict[str, Any]]


def _noop_prober(_upstream: UpstreamSettings | None) -> dict[str, Any]:
    """Default prober: no network I/O, always reports ok.

    The veops/remote-http task replaces this with a real (bounded,
    read-only) reachability check.
    """
    return {"status": "ok"}


def check_liveness() -> dict[str, Any]:
    """Report whether the process is alive.

    Always ``{"status": "ok"}``.  This probe must never fail once the
    process is running, including while it is draining during shutdown.
    """
    return {"status": "ok"}


def check_readiness(
    settings: Settings,
    *,
    upstream: UpstreamSettings | None = None,
    prober: UpstreamProber | None = None,
) -> dict[str, Any]:
    """Report whether the process is ready to serve traffic.

    Checks (in order, first failure wins):

    1. Inbound configuration validity (:func:`validate_settings`):
       a non-loopback bind requires ``MCP_AUTH_TOKEN``.
    2. Upstream configuration validity, when an
       :class:`UpstreamSettings` is supplied (http(s) base URL).
    3. The upstream reachability probe (injectable; no-op by default).

    Returns a dict with ``status`` = ``"ok"`` or ``"unavailable"`` and a
    ``reason`` field on failure.  No exception escapes: a failure to
    build the probe result is itself reported as unavailable, so a probe
    bug can never take the readiness endpoint down.
    """
    probe = prober if prober is not None else _noop_prober
    try:
        validate_settings(settings)
        if upstream is not None:
            validate_upstream_settings(upstream)
    except Exception as err:  # noqa: BLE001 - map any validation error
        return {"status": "unavailable", "reason": f"configuration: {err}"}
    try:
        result = probe(upstream)
    except Exception as err:  # noqa: BLE001 - a probe failure is unavailable
        return {"status": "unavailable", "reason": f"upstream probe failed: {err}"}
    status = result.get("status")
    if status == "ok":
        return {"status": "ok"}
    return {
        "status": "unavailable",
        "reason": str(result.get("reason", "upstream unavailable")),
    }
