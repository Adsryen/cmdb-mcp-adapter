"""Structured JSON logging to ``stderr``.

The MVP uses only the standard library: a custom
:class:`logging.Formatter` emits one JSON object per line on ``stderr``
(the stream that never carries MCP protocol frames).  No third-party
logging library is introduced, keeping the dependency footprint minimal.

Each log line contains at least::

    {"timestamp": ..., "level": ..., "logger": ..., "message": ...,
     "extra": {...}}

Callers may add structured fields via ``extra=`` when logging; those
fields land under ``extra`` in the JSON line and are run through the
redaction filter (see :mod:`cmdb_mcp_adapter.observability.redaction`)
before serialization, so secrets never reach the wire.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterable
from typing import Any

from cmdb_mcp_adapter.core import ConfigurationError
from cmdb_mcp_adapter.observability.redaction import RedactingFilter

_LOG_FORMAT_VERSION = 1

#: The stdlib LogRecord attributes that are *not* user-supplied ``extra``.
_STANDARD_ATTRS = frozenset(
    (
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "taskName",
        "message",
    )
)


def redaction_key(value: object) -> str | None:
    """Return a stable, non-secret key for a value.

    Secrets have no stable identity that is safe to log, so their
    redaction key is ``None`` (callers should simply not log them at
    all).  Strings that are not secret-shaped are returned as-is for
    debugging; anything longer than 64 chars is truncated.
    """
    if value is None:
        return None
    if isinstance(value, (int, float, bool)):
        return str(value)
    text = str(value)
    if len(text) > 64:
        return text[:64]
    return text


class _JsonFormatter(logging.Formatter):
    """Emit each record as a single JSON line (timestamp/level/logger)."""

    def format(self, record: logging.LogRecord) -> str:
        extra: dict[str, object] = {}
        for key, value in record.__dict__.items():
            if key in _STANDARD_ATTRS:
                continue
            extra[key] = value
        payload: dict[str, object] = {
            "timestamp": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S%z")
            or self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0] is not None:
            payload["exception"] = self.formatException(record.exc_info)
        if extra:
            payload["extra"] = extra
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(
    level: str = "INFO",
    *,
    stream: Any | None = None,
    secret_values: Iterable[str] | None = None,
    force: bool = True,
) -> None:
    """Configure the root logger for structured JSON output on ``stderr``.

    Arguments:
        level: One of the stdlib level names (case-insensitive).
            Invalid values raise :class:`ConfigurationError` (fail closed
            at startup — a misconfigured log level must not be silently
            downgraded).
        stream: The stream to write to (defaults to ``sys.stderr``).
        secret_values: Known secret strings (e.g. the configured tokens)
            that must be redacted even if they are not token-shaped.
        force: When ``True`` (default) any pre-existing handlers on the
            root logger are removed first, so repeated calls (tests,
            re-entry) do not duplicate lines.

    The redaction filter is attached to the root logger; handlers added
    later by other code still route records through it because filters on
    a logger apply to records handled by that logger's handlers.
    """
    import logging as _logging

    valid = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
    numeric_level = valid.get(level.upper())
    if numeric_level is None:
        raise ConfigurationError(
            f"invalid log level {level!r} (expected one of DEBUG/INFO/WARNING/ERROR/CRITICAL)"
        )

    root = _logging.getLogger()
    if force:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()

    out = stream if stream is not None else sys.stderr
    handler = _logging.StreamHandler(out)
    handler.setFormatter(_JsonFormatter())
    # The redaction filter is installed on the *handler* (not just the
    # root logger) because the stdlib logging pipeline only applies a
    # logger's own filters to its records — a record from a child logger
    # (e.g. ``cmdb_mcp_adapter.core``) walks up the hierarchy to reach
    # the root handler but is *not* passed through the root logger's
    # filters.  Putting the filter on the handler guarantees every
    # record is sanitized before it is formatted and written.
    handler.addFilter(RedactingFilter(list(secret_values or ())))
    root.addHandler(handler)
    root.setLevel(numeric_level)

    # ``httpx`` logs every outbound request URL at INFO, and the veops adapter
    # authenticates upstream calls by signing the query string (``_key`` /
    # ``_secret``).  The redaction filter only knows the *exact*, statically
    # configured secrets, so the per-request signature can never be registered
    # with it and would otherwise be written to stderr verbatim (replayable for
    # that path + params, and usable for offline dictionary checks against the
    # secret).  Pin the third-party logger to WARNING: connect/timeout errors
    # still surface, the INFO request lines do not.  The adapter's own
    # structured ``veops.request`` / ``veops.response`` events log the path
    # plus a sanitized params mapping (no signature).
    _logging.getLogger("httpx").setLevel(_logging.WARNING)
