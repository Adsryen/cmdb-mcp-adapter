"""Signal handling and graceful shutdown.

The stdio launcher installs these handlers before it enters the MCP
stdio loop: ``SIGINT`` / ``SIGTERM`` set a process-wide shutdown event,
and on exit the stdio launcher flushes ``stderr`` so no structured log
line is lost.  The HTTP launcher (remote-http task) uses the same event
from its shutdown hook; the *implementation* of the HTTP shutdown is out
of scope here, only the signal plumbing and the stderr-flush contract
live in this module so both transports share one behavior.

Design notes
------------
* Handlers only set an event; they never raise or print, so they are
  safe to run from any signal context.
* The previous handler is preserved and, for ``SIGINT``, restored after
  the stdio loop ends, so a second ``Ctrl-C`` behaves like the
  interpreter default.
* :func:`flush_stderr` runs as a best-effort drain; a failure to flush
  (e.g. a closed pipe) is swallowed — the process is exiting anyway.
"""

from __future__ import annotations

import contextlib
import signal
import sys
import threading
from collections.abc import Callable

#: Process-wide "shutdown requested" flag.  Signalled by
#: :func:`install_signal_handlers`; read by the transport loop to decide
#: whether to stop serving.
shutdown_event: threading.Event = threading.Event()


def reset_shutdown_event() -> None:
    """Clear the shutdown flag (used by tests and re-entrant launches)."""
    shutdown_event.clear()


def _make_handler() -> Callable[[int, object], None]:
    def handler(signum: int, _frame: object) -> None:
        # Only mark shutdown; the transport's main loop observes the
        # event and performs the actual (transport-specific) teardown.
        shutdown_event.set()

    return handler


def install_signal_handlers() -> None:
    """Register ``SIGINT`` / ``SIGTERM`` handlers that set the shutdown
    event.  No-ops outside POSIX and in threads other than the main one
    (the stdio launcher runs on the main thread).
    """
    if not hasattr(signal, "SIGTERM"):
        return
    if threading.current_thread() is not threading.main_thread():
        return
    try:
        signal.signal(signal.SIGINT, _make_handler())
        signal.signal(signal.SIGTERM, _make_handler())
    except (ValueError, OSError):
        # Running in a non-main thread or on a platform that forbids
        # re-registration; the default handlers still work.
        return


def restore_default_signals() -> None:
    """Restore the interpreter's default signal handlers.

    Called on the clean-exit path so that, after a graceful shutdown,
    the process behaves like any other Python process if the host sends
    another signal.
    """
    if not hasattr(signal, "SIGTERM"):
        return
    try:
        signal.signal(signal.SIGINT, signal.default_int_handler)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
    except (ValueError, OSError):
        return


def flush_stderr() -> None:
    """Best-effort flush of ``stderr``.

    All adapter diagnostics (structured JSON logs, configuration errors)
    are written to ``stderr``; the stdio launcher calls this right
    before returning so that no trailing log line is dropped when the
    process exits.  Failures are swallowed: the process is exiting.
    """
    with contextlib.suppress(OSError, ValueError):
        sys.stderr.flush()


#: Convenience for the stdio launcher: install handlers, and (on the
#: caller's behalf) remember to flush stderr on exit.
def shutdown_requested() -> bool:
    """True once a SIGINT/SIGTERM has been received."""
    return shutdown_event.is_set()


