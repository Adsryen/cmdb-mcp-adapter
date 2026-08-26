"""Correlation IDs and structured request/operation logging.

A correlation ID is a 128-bit uuid4 (hex) created per top-level operation
(an MCP tool call).  It is propagated through the core service via a
context variable so that every log line emitted during that operation —
adapter errors, retries, timeouts — can be correlated in the log stream.

The core service uses :func:`log_operation` at the entry of each method
to emit one structured line per operation with the fields the spec
requires: ``correlation_id``, ``tool`` (operation name), ``adapter``,
``duration_ms``, and ``outcome`` (``"ok"`` or the stable error-category
name).  The tool name is passed in by the MCP tool layer; the core falls
back to the method name when it is not supplied.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import time
import uuid
from collections.abc import Callable, Iterator
from typing import Any, TypeVar

from cmdb_mcp_adapter.observability.redaction import redact

#: Context variable holding the active correlation ID (hex string).
current_correlation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "cmdb_mcp_correlation_id", default=None
)

T = TypeVar("T")


def new_correlation_id() -> str:
    """Create a fresh 128-bit correlation ID (uuid4, hex)."""
    return uuid.uuid4().hex


def bind_correlation_id(cid: str | None = None) -> contextvars.Token[str | None]:
    """Bind a correlation ID to the current context; return the reset token.

    The caller (the MCP tool layer) is responsible for calling
    ``token.reset()`` in a ``finally`` block when the invocation is
    complete.  ``None`` creates a fresh ID.
    """
    value = cid or new_correlation_id()
    return current_correlation_id.set(value)


@contextlib.contextmanager
def with_correlation(correlation_id: str | None = None) -> Iterator[str]:
    """Temporarily bind a correlation ID to the current context.

    When ``correlation_id`` is supplied it is used as-is.  When it is
    ``None``, a *nested* call (one that is entered while an outer
    ``with_correlation`` is active) inherits the outer ID; a *top-level*
    call creates a fresh one.  The ID is restored when the context
    manager exits, so concurrent operations (asyncio tasks) do not
    cross-contaminate.
    """
    existing = current_correlation_id.get()
    cid = correlation_id or existing or new_correlation_id()
    token = current_correlation_id.set(cid)
    try:
        yield cid
    finally:
        current_correlation_id.reset(token)


def _outcome_for(error: BaseException | None) -> str:
    """Map an exception to a stable outcome string.

    ``None`` -> ``"ok"``.  A core error -> its stable category name.
    Anything else -> ``"internal"`` (the ``normalize`` fallback category),
    matching :func:`cmdb_mcp_adapter.core.errors.normalize`.
    """
    if error is None:
        return "ok"
    from cmdb_mcp_adapter.core.errors import CoreError, ErrorCategory

    if isinstance(error, CoreError):
        return error.category.value
    return ErrorCategory.INTERNAL.value


def log_operation(
    fn: Callable[..., T],
    *,
    tool: str | None = None,
    adapter_name: str,
    logger: logging.Logger | None = None,
    extra: dict[str, Any] | None = None,
) -> T:
    """Wrap a core-service operation with a correlation ID and a log line.

    Emits a single structured INFO (or ERROR, on failure) log record with
    the keys ``correlation_id``, ``tool``, ``adapter``, ``duration_ms``,
    and ``outcome``.  The correlation ID is bound to the context variable
    for the duration of the call so nested logging can reference it.

    The wrapper never swallows or alters exceptions; it only observes
    them to compute the outcome.  Tool input/output values are *not*
    logged (they may contain secrets); only the metadata above.
    """
    log = logger if logger is not None else logging.getLogger("cmdb_mcp_adapter.core")
    tool_name = tool if tool is not None else fn.__name__
    with with_correlation() as cid:
        log.info(
            "operation start",
            extra={
                "correlation_id": cid,
                "tool": tool_name,
                "adapter": adapter_name,
                "event": "operation.start",
            },
        )
        started = time.perf_counter()
        error: BaseException | None = None
        try:
            result = fn()
            return result
        except BaseException as exc:  # noqa: BLE001 - observation only, re-raise
            error = exc
            raise
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000.0, 3)
            outcome = _outcome_for(error)
            fields: dict[str, Any] = {
                "correlation_id": cid,
                "tool": tool_name,
                "adapter": adapter_name,
                "duration_ms": duration_ms,
                "outcome": outcome,
                "event": "operation.end",
            }
            if error is not None:
                # The error category is safe; the message is redacted in
                # case it carries an upstream detail with secrets.
                fields["error"] = redact(str(error))
            if extra:
                # Caller-supplied extras are redacted as well.
                fields["extra"] = redact(extra)
            level = logging.ERROR if error is not None else logging.INFO
            log.log(level, "operation end", extra=fields)

