"""Stable error categories for the unified CMDB core.

Adapters map vendor-specific failures onto these classes so that the core
service and the MCP tool layer can react uniformly without knowing anything
about the underlying CMDB.  Each error carries a stable :class:`ErrorCategory`
that is safe to surface to MCP clients, a human-readable ``message``, and an
optional ``upstream_detail`` that holds the raw vendor error.

Important boundary rule: ``upstream_detail`` is **never** emitted to MCP
clients.  The tool layer only exposes the category and the message.  This keeps
vendor internals, credentials, and raw payloads out of model context.
"""

from __future__ import annotations

from enum import Enum


class ErrorCategory(str, Enum):
    """Stable, vendor-neutral error categories.

    The string values are part of the public MCP contract: tool error text
    includes the category name.  New categories may be added, but existing
    values must not change meaning.
    """

    VALIDATION = "validation"
    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    NOT_FOUND = "not_found"
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    UPSTREAM_SERVER = "upstream_server"
    UNSUPPORTED_OPERATION = "unsupported_operation"
    CONFIGURATION = "configuration"
    INTERNAL = "internal"


class CoreError(Exception):
    """Base class for all normalized core errors.

    Attributes:
        category: The stable :class:`ErrorCategory` for this error.
        message: Human-readable description safe to surface to clients.
        upstream_detail: Optional raw vendor error (string or mapping).  Never
            forwarded to MCP clients.
    """

    category: ErrorCategory = ErrorCategory.INTERNAL

    def __init__(
        self,
        message: str,
        upstream_detail: object | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.upstream_detail = upstream_detail

    def describe(self) -> str:
        """A single client-safe line: ``<category>: <message>``."""
        return f"{self.category.value}: {self.message}"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.describe()


class ValidationError(CoreError):
    """Input failed core validation (malformed or out-of-contract arguments)."""

    category = ErrorCategory.VALIDATION


class ConfigurationError(CoreError):
    """Configuration is missing or invalid.  Raised at startup.

    Startup-time configuration errors must cause the process to exit non-zero
    before any protocol activity.
    """

    category = ErrorCategory.CONFIGURATION


class AuthenticationError(CoreError):
    """The upstream rejected the adapter's credentials (typically HTTP 401)."""

    category = ErrorCategory.AUTHENTICATION


class AuthorizationError(CoreError):
    """The upstream denied the requested action (typically HTTP 403)."""

    category = ErrorCategory.AUTHORIZATION


class NotFoundError(CoreError):
    """The requested resource does not exist upstream (typically HTTP 404)."""

    category = ErrorCategory.NOT_FOUND


class TimeoutError(CoreError):
    """The upstream did not respond within the configured timeout."""

    category = ErrorCategory.TIMEOUT


class RateLimitError(CoreError):
    """The upstream throttled the request (typically HTTP 429)."""

    category = ErrorCategory.RATE_LIMIT


class UpstreamServerError(CoreError):
    """The upstream returned a 5xx response or otherwise failed internally."""

    category = ErrorCategory.UPSTREAM_SERVER


class UnsupportedOperationError(CoreError):
    """The adapter does not implement this operation for the current model.

    Adapters are allowed to support only a subset of the operations; they must
    signal a missing capability with this error rather than a generic failure.
    """

    category = ErrorCategory.UNSUPPORTED_OPERATION


class InternalError(CoreError):
    """An unexpected, uncategorized failure inside the core or an adapter."""

    category = ErrorCategory.INTERNAL


def normalize(error: BaseException) -> CoreError:
    """Coerce an arbitrary exception into a :class:`CoreError`.

    Adapters should raise :class:`CoreError` subclasses directly.  This helper
    is a defensive fallback for the core service so that a stray, non-core
    exception from an adapter is still surfaced with a stable ``internal``
    category instead of an unhandled traceback.
    """
    if isinstance(error, CoreError):
        return error
    return InternalError(f"Unexpected error: {error}", upstream_detail=str(error))
