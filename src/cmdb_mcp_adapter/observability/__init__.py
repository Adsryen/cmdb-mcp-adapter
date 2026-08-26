"""Observability utilities: structured logging, secret redaction,
correlation IDs, and the bounded-retry policy.

These helpers are consumed by the transport launchers (which configure
logging at startup) and by the core service / adapter layer (which uses
correlation IDs and the retry policy).  They deliberately depend only on
the standard library.
"""

from cmdb_mcp_adapter.observability.correlation import (
    bind_correlation_id,
    current_correlation_id,
    new_correlation_id,
    with_correlation,
)
from cmdb_mcp_adapter.observability.logging import (
    configure_logging,
    redaction_key,
)
from cmdb_mcp_adapter.observability.redaction import (
    REDACTED,
    RedactingFilter,
    looks_like_secret,
    redact,
)
from cmdb_mcp_adapter.observability.retry import (
    RETRYABLE_CATEGORIES,
    RETRYABLE_EXCEPTIONS,
    retry,
)

__all__ = [
    "REDACTED",
    "RETRYABLE_CATEGORIES",
    "RETRYABLE_EXCEPTIONS",
    "RedactingFilter",
    "bind_correlation_id",
    "configure_logging",
    "current_correlation_id",
    "looks_like_secret",
    "new_correlation_id",
    "redact",
    "redaction_key",
    "retry",
    "with_correlation",
]
