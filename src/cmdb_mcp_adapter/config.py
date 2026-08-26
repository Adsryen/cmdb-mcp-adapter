"""Typed runtime configuration for the CMDB MCP adapter.

Configuration sources, in deterministic precedence order (highest first):

1. **CLI flags** — the ``http`` subcommand's ``--host`` / ``--port`` override
   the environment; stdio has no such flags (it only speaks on stdio, so a
   network bind is meaningless there).
2. **Environment variables** — the primary source for the MVP (see the
   variable tables in ``docs/configuration-security-observability.md``).
3. **Safe defaults** — loopback bind, no auth token, conservative timeouts.

The MVP deliberately does **not** read configuration files or a secret
manager: every value is an environment variable or a CLI flag.  This keeps
the trust model simple — secrets exist only in the process environment and
are rotated by changing the environment and restarting the process.

Secret values (``MCP_AUTH_TOKEN``, ``VEOPS_SECRET``) must never be written
into repository files; the examples/ directory (once it exists) documents
*variable names only* and is never consulted by this module.  Nothing in
this package opens a file to obtain configuration.

The two credential domains are intentionally independent:

* **Inbound authentication** — ``MCP_AUTH_TOKEN`` authenticates *clients*
  of this adapter's HTTP endpoint (see
  :mod:`cmdb_mcp_adapter.server.auth`).
* **Upstream credentials** — the ``VEOPS_*`` variables authenticate this
  adapter *toward* the VeOps CMDB (see :class:`UpstreamSettings` /
  :func:`load_upstream_settings`; the veops adapter task owns the client).
  Since 0.2.0 the VeOps CMDB (OneOps 2.6.2) authenticates with a
  per-user ``key``/``secret`` pair (``VEOPS_KEY`` / ``VEOPS_SECRET``);
  the pre-0.2.0 ``VEOPS_TOKEN`` variable is retained only to detect
  legacy deployments and produce a migration error at startup.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from cmdb_mcp_adapter.core import ConfigurationError

DEFAULT_BIND_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_ADAPTER = "veops"
DEFAULT_HTTP_TIMEOUT_SECONDS = 30
DEFAULT_HTTP_MAX_RETRIES = 2
DEFAULT_LOG_LEVEL = "INFO"

#: Bind addresses that count as "loopback" and therefore do not require an
#: inbound auth token.  Any other bind address fails closed.
_LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "::1", "localhost"})

# Environment variable names.  The ``MCP_*`` set is the inbound/runtime
# domain; the ``VEOPS_*`` set is the upstream CMDB domain.  The split is a
# contract: the two domains never share a value and must be rotated
# independently.
ENV_BIND_HOST = "MCP_BIND_HOST"
ENV_PORT = "MCP_PORT"
ENV_ADAPTER = "MCP_ADAPTER"
ENV_AUTH_TOKEN = "MCP_AUTH_TOKEN"
ENV_HTTP_TIMEOUT = "MCP_HTTP_TIMEOUT"
ENV_HTTP_MAX_RETRIES = "MCP_HTTP_MAX_RETRIES"
ENV_LOG_LEVEL = "MCP_LOG_LEVEL"
ENV_VEOPS_BASE_URL = "VEOPS_BASE_URL"
ENV_VEOPS_KEY = "VEOPS_KEY"
ENV_VEOPS_SECRET = "VEOPS_SECRET"
#: Legacy (pre-0.2.0) single-token variable.  Read for *diagnosis only*:
#: the veops adapter fails closed with a migration hint when it is set
#: while ``VEOPS_KEY``/``VEOPS_SECRET`` are missing.  Never used to
#: authenticate.
ENV_VEOPS_TOKEN = "VEOPS_TOKEN"

#: Every environment variable the adapter understands.  Tests and secret
#: scans use this as the single source of truth.
ALL_ENV_VARS: frozenset[str] = frozenset(
    {
        ENV_BIND_HOST,
        ENV_PORT,
        ENV_ADAPTER,
        ENV_AUTH_TOKEN,
        ENV_HTTP_TIMEOUT,
        ENV_HTTP_MAX_RETRIES,
        ENV_LOG_LEVEL,
        ENV_VEOPS_BASE_URL,
        ENV_VEOPS_KEY,
        ENV_VEOPS_SECRET,
        ENV_VEOPS_TOKEN,
    }
)

_VALID_LOG_LEVELS: frozenset[str] = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})

_TOKEN_VALUE_PATTERN = re.compile(r"(?i)^(?:bearer\s+)?[a-z0-9+/_-]{16,}$")


@dataclass(frozen=True)
class Settings:
    """Immutable runtime settings shared by both transports.

    This is the *inbound* configuration domain: where the adapter listens
    and how clients authenticate to it.  Upstream CMDB credentials live in
    :class:`UpstreamSettings`.
    """

    bind_host: str = DEFAULT_BIND_HOST
    port: int = DEFAULT_PORT
    adapter: str = DEFAULT_ADAPTER
    auth_token: str | None = None
    http_timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SECONDS
    http_max_retries: int = DEFAULT_HTTP_MAX_RETRIES
    log_level: str = DEFAULT_LOG_LEVEL

    def is_loopback(self) -> bool:
        """True when the bind address is a loopback address.

        A loopback bind only accepts connections from the local machine, so
        the MVP allows running without an auth token.  Any non-loopback
        bind exposes the adapter to the network and requires
        ``MCP_AUTH_TOKEN`` (see :func:`validate_settings`).
        """
        return self.bind_host in _LOOPBACK_HOSTS


def _normalize_token(raw: str | None) -> str | None:
    """Normalize a raw token value: empty/whitespace-only becomes ``None``.

    This keeps the load-time representation clean so that a token that
    was set but is effectively empty is indistinguishable from an
    unset one (both fail closed identically for a non-loopback bind).
    """
    if raw is None:
        return None
    if not raw.strip():
        return None
    return raw


def _non_negative_int(env: os._Environ[str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigurationError(
            f"Invalid {name} value: {raw!r} (expected a non-negative integer)"
        ) from None
    if value < 0:
        raise ConfigurationError(f"Invalid {name} value: {raw!r} (must be >= 0)")
    return value


def _port(env: os._Environ[str]) -> int:
    raw = env.get(ENV_PORT)
    if raw is None:
        return DEFAULT_PORT
    try:
        value = int(raw)
    except ValueError:
        msg = f"Invalid {ENV_PORT} value: {raw!r} (expected an integer)"
        raise ConfigurationError(msg) from None
    if value < 1 or value > 65535:
        msg = f"Invalid {ENV_PORT} value: {raw!r} (must be 1-65535)"
        raise ConfigurationError(msg)
    return value


def _positive_float(env: os._Environ[str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        msg = f"Invalid {name} value: {raw!r} (expected a positive number)"
        raise ConfigurationError(msg) from None
    if value <= 0:
        msg = f"Invalid {name} value: {raw!r} (must be > 0)"
        raise ConfigurationError(msg)
    return value


def _env_level(env: os._Environ[str]) -> str:
    raw = env.get(ENV_LOG_LEVEL, DEFAULT_LOG_LEVEL)
    level = raw.upper()
    if level not in _VALID_LOG_LEVELS:
        raise ConfigurationError(
            f"Invalid {ENV_LOG_LEVEL} value: {raw!r} "
            f"(expected one of {sorted(_VALID_LOG_LEVELS)})"
        )
    return level


def load_settings(env: os._Environ[str] | None = None) -> Settings:
    """Load the inbound :class:`Settings` from environment variables.

    Recognised variables: ``MCP_BIND_HOST``, ``MCP_PORT``, ``MCP_ADAPTER``,
    ``MCP_AUTH_TOKEN``, ``MCP_HTTP_TIMEOUT``, ``MCP_HTTP_MAX_RETRIES``, and
    ``MCP_LOG_LEVEL``.  CLI flags supplied by the ``http`` subcommand are
    applied by the CLI layer *after* this function (highest precedence).
    The auth token is only ever read from the environment; it must never
    be written into repository files or log output.

    Raises:
        ConfigurationError: if a value is syntactically invalid (bad port,
            bad timeout, unknown log level).  Callers must treat this as a
            fatal startup condition (fail closed).
    """
    if env is None:
        env = os.environ
    return Settings(
        bind_host=env.get(ENV_BIND_HOST, DEFAULT_BIND_HOST),
        port=_port(env),
        adapter=env.get(ENV_ADAPTER, DEFAULT_ADAPTER),
        auth_token=_normalize_token(env.get(ENV_AUTH_TOKEN)),
        http_timeout_seconds=_positive_float(env, ENV_HTTP_TIMEOUT, DEFAULT_HTTP_TIMEOUT_SECONDS),
        http_max_retries=_non_negative_int(env, ENV_HTTP_MAX_RETRIES, DEFAULT_HTTP_MAX_RETRIES),
        log_level=_env_level(env),
    )


def apply_cli_overrides(
    settings: Settings,
    *,
    host: str | None = None,
    port: int | None = None,
) -> Settings:
    """Apply CLI flag overrides on top of env-loaded settings.

    CLI flags have the highest precedence in the chain (CLI > env >
    defaults); ``None`` values leave the existing (env-derived) value in
    place.  Only the ``http`` subcommand exposes these flags.
    """
    return Settings(
        bind_host=host if host is not None else settings.bind_host,
        port=port if port is not None else settings.port,
        adapter=settings.adapter,
        auth_token=settings.auth_token,
        http_timeout_seconds=settings.http_timeout_seconds,
        http_max_retries=settings.http_max_retries,
        log_level=settings.log_level,
    )


def _validate_scheme(url: str, field_name: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ConfigurationError(
            f"Invalid {field_name} value: {url!r} (must be an http(s) URL)"
        )
    if not parts.netloc:
        raise ConfigurationError(f"Invalid {field_name} value: {url!r} (missing host)")


@dataclass(frozen=True)
class UpstreamSettings:
    """The *upstream* CMDB credential domain (VeOps in the MVP).

    This domain is fully independent from the inbound domain
    (:class:`Settings`): ``MCP_AUTH_TOKEN`` authenticates *clients* of
    this adapter, while the ``VEOPS_*`` values authenticate the adapter
    *toward* the CMDB.  They must be rotated independently and never
    mixed.

    The veops adapter task owns the actual HTTP client and enforces a
    *stricter* fail-closed policy on top of these values (``base_url``,
    ``key`` and ``secret`` all mandatory); see
    :mod:`cmdb_mcp_adapter.adapters.veops.config`.  Configuration files
    and secret managers are explicitly out of scope.

    Attributes:
        base_url: The CMDB base URL (``http(s)``), or ``None``.
        key: The VeOps user key (``VEOPS_KEY``), or ``None``.
        secret: The VeOps user secret (``VEOPS_SECRET``), or ``None``.
        token: Legacy ``VEOPS_TOKEN`` value, read for diagnosis only
            (pre-0.2.0 deployments); never used to authenticate.
    """

    base_url: str | None = None
    key: str | None = None
    secret: str | None = None
    token: str | None = None

    def redaction_secrets(self) -> tuple[str, ...]:
        """Credential values that must never appear in logs or
        diagnostics (see :mod:`cmdb_mcp_adapter.observability.redaction`).

        The key is an *identity*, but it is treated as a secret here for
        defense in depth: it rides on every signed request, so any log
        line or error body that echoes request material can carry it
        back.  The legacy ``token`` (0.1.0 deployments) is included too
        — an old environment may still hold a real 0.1.0 token whose
        value must stay scrubbed.
        """
        return tuple(value for value in (self.key, self.secret, self.token) if value)


def load_upstream_settings(env: os._Environ[str] | None = None) -> UpstreamSettings:
    """Load upstream CMDB (VeOps) settings from environment variables.

    Recognised variables: ``VEOPS_BASE_URL`` (must be an http(s) URL when
    set), ``VEOPS_KEY`` / ``VEOPS_SECRET`` (the VeOps 2.6.2 user
    credential pair used for per-request signing), and the legacy
    ``VEOPS_TOKEN`` (read for diagnosis only; the veops adapter turns it
    into a migration error when the new pair is missing).  The loader
    itself treats all values as optional so that the *generic* upstream
    domain can be represented even when a specific adapter does not need
    them; the veops adapter enforces the mandatory set (fail closed)
    via its own validation.  Credential values are never written into
    repository files.

    Raises:
        ConfigurationError: if ``VEOPS_BASE_URL`` is set but not a valid
            http(s) URL (fail closed at startup).
    """
    if env is None:
        env = os.environ
    base_url = env.get(ENV_VEOPS_BASE_URL) or None
    if base_url is not None:
        _validate_scheme(base_url, ENV_VEOPS_BASE_URL)
    return UpstreamSettings(
        base_url=base_url,
        key=env.get(ENV_VEOPS_KEY) or None,
        secret=env.get(ENV_VEOPS_SECRET) or None,
        token=env.get(ENV_VEOPS_TOKEN) or None,
    )


def validate_upstream_settings(upstream: UpstreamSettings) -> None:
    """Validate an :class:`UpstreamSettings` instance (idempotent).

    Raises:
        ConfigurationError: if ``base_url`` is present but not http(s).
    """
    if upstream.base_url is not None:
        _validate_scheme(upstream.base_url, ENV_VEOPS_BASE_URL)


def validate_settings(settings: Settings) -> None:
    """Startup validation for the inbound domain (fail closed).

    Rules:

    * A bind address that is **not** a loopback address requires
      ``MCP_AUTH_TOKEN`` to be configured; otherwise the process must
      refuse to start.
    * A configured auth token must be non-empty and non-whitespace, and
      must not look like a *value pattern* (e.g. an unexpanded shell
      template ``${VEOPS_TOKEN}`` or a placeholder) — such values are
      almost certainly misconfiguration and would fail open silently.

    Raises:
        ConfigurationError: on any rule violation.  The transport
        launchers surface the error on stderr and exit non-zero before
        any protocol activity.
    """
    if settings.port < 1 or settings.port > 65535:
        raise ConfigurationError(
            f"{ENV_PORT} must be in the range 1-65535 (got {settings.port})"
        )
    if not settings.is_loopback() and not settings.auth_token:
        raise ConfigurationError(
            f"binding to non-loopback address '{settings.bind_host}' requires "
            f"{ENV_AUTH_TOKEN} to be configured (public/remote exposure is not "
            "permitted without inbound authentication)"
        )
    if settings.auth_token is not None:
        if not settings.auth_token.strip():
            raise ConfigurationError(f"{ENV_AUTH_TOKEN} is set but empty/whitespace")
        if _looks_like_value_pattern(settings.auth_token):
            raise ConfigurationError(
                f"{ENV_AUTH_TOKEN} looks like an unexpanded value pattern, "
                "not a literal token (check the process environment)"
            )


def _looks_like_value_pattern(value: str) -> bool:
    """True when a configured value looks like an unexpanded template.

    A literal token is a single opaque string; anything containing
    whitespace, shell parameter expansion (``${...}``), or a scheme
    prefix (``https://`` — someone pasted a URL as the token) is treated
    as misconfiguration rather than silently accepted.
    """
    if any(ch.isspace() for ch in value):
        return True
    if "${" in value:
        return True
    return _TOKEN_VALUE_PATTERN.match(value) is None and value.startswith(("http://", "https://"))
