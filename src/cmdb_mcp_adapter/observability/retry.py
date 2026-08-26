"""Bounded retry policy for idempotent upstream operations.

Rules (matching the design doc):

* **Only idempotent (read) operations are retried.**  Write operations
  (create/update/delete) are never retried — retrying a non-idempotent
  call risks duplicate side effects, so the first failure is returned as-is.
* **Only transient categories are retried:** timeout, rate limit, and
  upstream-server (5xx).  Authentication, authorization, validation,
  not-found, and configuration failures are *not* retried (retrying
  cannot fix them and only delays the client).
* **Bounded exponential backoff with jitter**, capped by
  ``backoff_cap_seconds``; the number of retries is bounded by
  ``max_retries`` (0 = no retry).
* **Category is preserved on exhaustion:** when retries run out the
  original exception (and therefore its stable category) is re-raised
  unchanged — a timeout stays a timeout.

The helper works on plain functions and is safe to apply around adapter
calls; it does not know about asyncio (the veops task may add an async
variant by wrapping this one) and does not sleep more than the computed
delay.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from typing import TypeVar

from cmdb_mcp_adapter.core.errors import CoreError, ErrorCategory

T = TypeVar("T")

#: Core error categories that are safe to retry (transient upstream
#: conditions).  These are the *only* categories retried.
RETRYABLE_CATEGORIES: frozenset[ErrorCategory] = frozenset(
    {
        ErrorCategory.TIMEOUT,
        ErrorCategory.RATE_LIMIT,
        ErrorCategory.UPSTREAM_SERVER,
    }
)

#: Exception classes matching the retryable categories.  Both the core
#: classes and the plain builtin ``TimeoutError`` are treated as timeout.
RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    CoreError,
    TimeoutError,
)


def is_retryable(error: BaseException) -> bool:
    """True when ``error`` represents a transient, retryable condition."""
    if isinstance(error, CoreError):
        return error.category in RETRYABLE_CATEGORIES
    # Plain builtin timeout (e.g. ``asyncio``/socket timeouts that were
    # not already normalized by the adapter) counts as timeout.
    return isinstance(error, TimeoutError) and not isinstance(error, CoreError)


def _backoff_delay(attempt: int, base_seconds: float, cap_seconds: float) -> float:
    """Exponential backoff with full jitter, capped.

    ``attempt`` is 0-based (0 = delay before the first retry).  The delay
    is a uniform random value in ``[0, min(base * 2**attempt, cap)]``.
    """
    exp = base_seconds * (2.0 ** attempt)
    bounded = min(exp, cap_seconds)
    return random.uniform(0.0, bounded)


def _sleep(seconds: float) -> None:
    if seconds > 0:
        time.sleep(seconds)


def retry(
    fn: Callable[[], T],
    *,
    is_idempotent: bool,
    max_retries: int,
    backoff_base_seconds: float = 0.5,
    backoff_cap_seconds: float = 30.0,
    sleep: Callable[[float], None] | None = None,
    logger: logging.Logger | None = None,
    operation_name: str = "",
) -> T:
    """Call ``fn`` with a bounded, idempotency-aware retry policy.

    Arguments:
        fn: Zero-argument callable performing one attempt (typically a
            bound adapter method invocation).
        is_idempotent: ``True`` for read operations.  When ``False`` the
            function is called exactly once and any exception is
            re-raised immediately — writes are never retried.
        max_retries: Maximum number of *additional* attempts after the
            first failure (0 disables retrying entirely).
        backoff_base_seconds: Base delay for the exponential backoff.
        backoff_cap_seconds: Upper bound on any single backoff delay.
        sleep: Sleep function (defaults to :func:`time.sleep`); inject a
            no-op in tests.
        logger: Optional logger for retry diagnostics.
        operation_name: Label used in retry log lines.

    Returns whatever ``fn`` returns.  On final failure the *original*
    exception is re-raised (category preserved).
    """
    log = logger if logger is not None else logging.getLogger("cmdb_mcp_adapter.retry")
    # Read the module-level sleep lazily on each call so tests can patch
    # ``_sleep`` (or pass an explicit ``sleep``) without capturing a stale
    # reference at definition time.
    do_sleep = sleep if sleep is not None else _sleep

    max_attempts = max(0, int(max_retries))
    attempt = 0
    while True:
        try:
            return fn()
        except BaseException as error:  # noqa: BLE001 - observed, then re-raised
            if not is_idempotent or not is_retryable(error):
                raise
            if attempt >= max_attempts:
                # Retry budget exhausted: re-raise the *original* error so
                # its stable category survives (a timeout stays a timeout).
                raise
            delay = _backoff_delay(attempt, backoff_base_seconds, backoff_cap_seconds)
            log.warning(
                "retrying operation",
                extra={
                    "operation": operation_name,
                    "attempt": attempt + 1,
                    "max_retries": max_attempts,
                    "category": error.category.value if isinstance(error, CoreError) else "timeout",
                    "delay_ms": round(delay * 1000.0, 1),
                    "event": "retry.scheduled",
                },
            )
            do_sleep(delay)
            attempt += 1
