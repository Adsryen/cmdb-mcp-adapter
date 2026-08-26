"""VeOps adapter configuration.

The veops adapter consumes the *upstream* credential domain defined by
:mod:`cmdb_mcp_adapter.config` (:class:`UpstreamSettings`, loaded from
``VEOPS_BASE_URL`` / ``VEOPS_KEY`` / ``VEOPS_SECRET``) and the *inbound*
:class:`Settings` (for the HTTP timeout and retry budget).  This module
adds the veops-specific policy that is stricter than the generic loader:

* ``VEOPS_BASE_URL`` is **required** -- the adapter is meaningless without
  an endpoint, so its absence is a startup configuration error.
* ``VEOPS_KEY`` and ``VEOPS_SECRET`` are **both required** -- VeOps 2.6.2
  authenticates service calls with a per-user key/secret pair signed
  per-request (SHA-1 over path + secret + sorted scalar parameter values;
  see :mod:`.signing`).  A single bearer token
  (the pre-0.2.0 ``VEOPS_TOKEN``) cannot express that, so 0.2.0 replaced
  it: if ``VEOPS_TOKEN`` is set while ``VEOPS_KEY``/``VEOPS_SECRET`` are
  missing, startup fails with a *migration* error rather than silently
  mis-authenticating.  Fail closed at startup, never fail open.

The base URL is normalized (trailing slash stripped) exactly once here so
that the client can concatenate path segments without worrying about
double slashes.
"""

from __future__ import annotations

from dataclasses import dataclass

from cmdb_mcp_adapter.config import (
    ENV_VEOPS_BASE_URL,
    ENV_VEOPS_KEY,
    ENV_VEOPS_SECRET,
    ENV_VEOPS_TOKEN,
    UpstreamSettings,
    validate_upstream_settings,
)
from cmdb_mcp_adapter.core import ConfigurationError

#: The VeOps API root path.  Verified against the OneOps/VeOps 2.6.2
#: deployment (2026-08): the real API is mounted at ``/api/v0.1``, not
#: ``/api/v1`` (which 404s).  Every endpoint path in :mod:`.client` is
#: relative to this.
API_PREFIX = "/api/v0.1"

#: The endpoint used by the reachability prober.  ``/ci_types`` is a real,
#: cheap, read-only endpoint: it answers 200 with valid credentials and
#: 401 without them, so the prober treats {200, 401, 403} as "reachable"
#: (the server answered; only its authentication state differs).
PROBE_PATH = "/ci_types"

#: Error text emitted when a pre-0.2.0 ``VEOPS_TOKEN`` value is found but
#: the new credential pair is missing.  Deliberately names both new
#: variables so the migration is a one-line fix in the process
#: environment.
_TOKEN_MIGRATION_MESSAGE = (
    f"{ENV_VEOPS_TOKEN} is not used by adapter version 0.2.0: the VeOps CMDB "
    f"authenticates with a per-user key/secret pair.  Set {ENV_VEOPS_KEY} and "
    f"{ENV_VEOPS_SECRET} instead (see docs/veops-adapter.md, "
    '"Authentication") and remove the legacy variable'
)


@dataclass(frozen=True)
class VeopsConfig:
    """Validated, normalized configuration for the veops client.

    Attributes:
        base_url: The upstream base URL with any trailing slash removed.
            Always non-empty and always ``http(s)`` (validated by the
            generic loader / :func:`validate_upstream_settings`).
        key: The VeOps user *key* (identity, sent as ``_key``).  Not
            itself a secret, but treated as sensitive in logs.
        secret: The VeOps user *secret* (signing material; never logged).
        timeout_seconds: Per-request timeout (from the inbound
            :attr:`Settings.http_timeout_seconds`).
        max_retries: The retry budget applied to *read* operations (from
            :attr:`Settings.http_max_retries`).
    """

    base_url: str
    key: str
    secret: str
    timeout_seconds: float
    max_retries: int


def validate_veops_upstream(upstream: UpstreamSettings) -> None:
    """Validate the *veops-specific* upstream policy (fail closed).

    Stricter than the generic :func:`validate_upstream_settings`:
    ``base_url``, ``key`` and ``secret`` are all mandatory for veops.

    Raises:
        ConfigurationError: if any value is missing, if the base URL is
            not a valid ``http(s)`` URL, or if a legacy ``VEOPS_TOKEN``
            value is present without the new credential pair (migration
            error with a fix hint).
    """
    if upstream.base_url is not None:
        validate_upstream_settings(upstream)
    if upstream.base_url is None:
        raise ConfigurationError(
            f"{ENV_VEOPS_BASE_URL} is required for the veops adapter "
            "(an http(s) base URL for the CMDB endpoint)"
        )
    key = (upstream.key or "").strip()
    secret = (upstream.secret or "").strip()
    if not key or not secret:
        if (upstream.token or "").strip():
            raise ConfigurationError(_TOKEN_MIGRATION_MESSAGE)
        missing = (
            ENV_VEOPS_KEY if not key else ENV_VEOPS_SECRET
        )
        raise ConfigurationError(
            f"{ENV_VEOPS_KEY} and {ENV_VEOPS_SECRET} are required for the "
            "veops adapter: VeOps 2.6.2 authenticates service calls with a "
            f"per-user key/secret pair (missing/empty: {missing}).  Get them "
            "from the VeOps user-management page (or the ``users`` table "
            "``key``/``secret`` columns).  The adapter refuses to start "
            "without them (fail closed)"
        )


def build_veops_config(
    upstream: UpstreamSettings, timeout_seconds: float, max_retries: int
) -> VeopsConfig:
    """Build a normalized :class:`VeopsConfig`.

    ``timeout_seconds`` and ``max_retries`` come from the inbound
    :class:`Settings` (``MCP_HTTP_TIMEOUT`` / ``MCP_HTTP_MAX_RETRIES``) so
    that the veops client actually honours those previously-unconsumed
    values.

    Raises:
        ConfigurationError: if the veops-specific policy is not satisfied
            (see :func:`validate_veops_upstream`).
    """
    validate_veops_upstream(upstream)
    # All values are guaranteed non-None / non-empty past validation.
    assert upstream.base_url is not None
    assert upstream.key is not None
    assert upstream.secret is not None
    return VeopsConfig(
        base_url=upstream.base_url.rstrip("/"),
        key=upstream.key,
        secret=upstream.secret,
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
    )
