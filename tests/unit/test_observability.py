"""Structured JSON logging, correlation IDs, and the bounded retry policy.

The logging tests capture ``stderr`` via ``capfd`` (file-descriptor
level) because the stdlib ``logging`` writes to the real file
descriptor, not the Python-level ``sys.stderr`` that ``capsys``
intercepts.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

import pytest

from cmdb_mcp_adapter.core import (
    CoreService,
    NotFoundError,
    RateLimitError,
    UpstreamServerError,
    ValidationError,
)
from cmdb_mcp_adapter.core import TimeoutError as CoreTimeoutError
from cmdb_mcp_adapter.observability import (
    REDACTED,
    RedactingFilter,
    bind_correlation_id,
    configure_logging,
    current_correlation_id,
    new_correlation_id,
    retry,
)
from cmdb_mcp_adapter.observability.correlation import with_correlation
from cmdb_mcp_adapter.observability.logging import redaction_key

FAKE_SECRET = "s3cret-value-0123456789abcdef"

# ---------------------------------------------------------------------------
# Correlation IDs
# ---------------------------------------------------------------------------
class TestCorrelation:
    def test_new_ids_are_unique_and_hex(self) -> None:
        a = new_correlation_id()
        b = new_correlation_id()
        assert a != b
        assert len(a) == 32
        int(a, 16)  # must parse as hex

    def test_with_correlation_sets_and_resets(self) -> None:
        assert current_correlation_id.get() is None
        with with_correlation() as cid:
            assert current_correlation_id.get() == cid
        assert current_correlation_id.get() is None

    def test_nested_inherits_outer(self) -> None:
        with with_correlation("outer") as outer, with_correlation() as inner:
            assert inner == outer
            assert current_correlation_id.get() == outer

    def test_bind_and_reset(self) -> None:
        token = bind_correlation_id()
        cid = current_correlation_id.get()
        assert cid is not None
        current_correlation_id.reset(token)
        assert current_correlation_id.get() is None


# ---------------------------------------------------------------------------
# Structured logging to stderr
# ---------------------------------------------------------------------------
class TestConfigureLogging:
    def test_json_line_on_stderr(self, capfd: pytest.CaptureFixture[str]) -> None:
        configure_logging("INFO", force=True)
        logging.getLogger("test.logger").info("hello world")
        sys.stderr.flush()
        out = capfd.readouterr()
        assert out.err
        lines = [ln for ln in out.err.splitlines() if ln.strip()]
        assert lines, "expected at least one JSON line on stderr"
        payload = json.loads(lines[-1])
        assert payload["level"] == "INFO"
        assert payload["logger"] == "test.logger"
        assert payload["message"] == "hello world"
        assert "timestamp" in payload
        logging.shutdown()

    def test_extra_fields_are_logged(self, capfd: pytest.CaptureFixture[str]) -> None:
        configure_logging("INFO", force=True)
        logging.getLogger("t").info(
            "op",
            extra={"correlation_id": "abc", "tool": "get_models", "duration_ms": 3.2},
        )
        sys.stderr.flush()
        out = capfd.readouterr()
        payload = json.loads([ln for ln in out.err.splitlines() if ln.strip()][-1])
        assert payload["extra"]["correlation_id"] == "abc"
        assert payload["extra"]["tool"] == "get_models"
        assert payload["extra"]["duration_ms"] == 3.2
        logging.shutdown()

    def test_invalid_level_fails_closed(self) -> None:
        from cmdb_mcp_adapter.core import ConfigurationError

        with pytest.raises(ConfigurationError):
            configure_logging("NOT_A_LEVEL")

    def test_level_filtering(self, capfd: pytest.CaptureFixture[str]) -> None:
        configure_logging("ERROR", force=True)
        logging.getLogger("t").info("should not appear")
        sys.stderr.flush()
        out = capfd.readouterr()
        assert "should not appear" not in out.err
        logging.shutdown()


# ---------------------------------------------------------------------------
# RedactingFilter integration with the logging pipeline
# ---------------------------------------------------------------------------
class TestRedactingFilter:
    def test_secret_in_message_is_redacted(self, capfd: pytest.CaptureFixture[str]) -> None:
        configure_logging(
            "INFO",
            force=True,
            secret_values=["hunter2-secret"],
        )
        logging.getLogger("t").info("token=hunter2-secret done")
        sys.stderr.flush()
        out = capfd.readouterr()
        assert "hunter2-secret" not in out.err
        assert REDACTED in out.err
        logging.shutdown()

    def test_secret_in_extra_is_redacted(self, capfd: pytest.CaptureFixture[str]) -> None:
        configure_logging("INFO", force=True, secret_values=[FAKE_SECRET])
        logging.getLogger("t").info("op", extra={"client_secret": FAKE_SECRET})
        sys.stderr.flush()
        out = capfd.readouterr()
        assert FAKE_SECRET not in out.err
        assert REDACTED in out.err
        logging.shutdown()

    def test_filter_always_returns_true(self) -> None:
        import logging as _logging

        f = RedactingFilter()
        record = _logging.LogRecord(
            "t", _logging.INFO, "path", 1, "msg with Bearer a1b2c3d4e5f6g7h8i9j0", None, None
        )
        assert f.filter(record) is True
        assert "Bearer" not in record.getMessage()

    def test_redaction_key(self) -> None:
        assert redaction_key(None) is None
        assert redaction_key(3) == "3"
        assert redaction_key("x" * 100) == "x" * 64


# ---------------------------------------------------------------------------
# log_operation: correlation + structured fields on CoreService
# ---------------------------------------------------------------------------
class _CountingAdapter:
    """A minimal adapter that records call counts and can inject errors."""

    def __init__(self) -> None:
        self.calls: dict[str, int] = {}
        self.next_error: Exception | None = None
        self.error_budget: int = 0

    def _record(self, op: str) -> None:
        self.calls[op] = self.calls.get(op, 0) + 1
        if self.next_error is not None and self.error_budget > 0:
            self.error_budget -= 1
            err, self.next_error = self.next_error, None
            raise err

    def list_models(self) -> list[Any]:
        self._record("list_models")
        return []

    def get_instances(
        self,
        model_name: str,
        *,
        page_size: int,
        cursor: str | None,
        filters: dict[str, Any],
    ) -> Any:
        self._record("get_instances")
        from cmdb_mcp_adapter.core.models import Page

        return Page(
            items=[], page=1, page_size=page_size, total=0, next_cursor=None
        )

    def create_instance(self, model_name: str, data: dict[str, Any]) -> Any:
        self._record("create_instance")
        from cmdb_mcp_adapter.core.models import CMDBInstance

        return CMDBInstance(model_name=model_name, instance_id="i-1", data=data)

    def update_instance(
        self,
        model_name: str,
        instance_id: str,
        data: dict[str, Any],
    ) -> Any:
        self._record("update_instance")
        from cmdb_mcp_adapter.core.models import CMDBInstance

        return CMDBInstance(
            model_name=model_name, instance_id=instance_id, data=data
        )

    def delete_instance(self, model_name: str, instance_id: str) -> None:
        self._record("delete_instance")

    def search_instances(
        self,
        model_name: str,
        query: str,
        *,
        page_size: int,
        cursor: str | None,
    ) -> Any:
        self._record("search_instances")
        from cmdb_mcp_adapter.core.models import Page

        return Page(
            items=[], page=1, page_size=page_size, total=0, next_cursor=None
        )

    def get_relations(self, model_name: str, instance_id: str) -> list[Any]:
        self._record("get_relations")
        return []


def _service_with_logging() -> tuple[CoreService, _CountingAdapter]:
    adapter = _CountingAdapter()
    return CoreService(adapter), adapter  # type: ignore[arg-type]


class TestLogOperation:
    def test_ok_emits_structured_fields(self, capfd: pytest.CaptureFixture[str]) -> None:
        configure_logging("INFO", force=True)
        service, adapter = _service_with_logging()
        service.list_models()
        sys.stderr.flush()
        out = capfd.readouterr()
        lines = [json.loads(ln) for ln in out.err.splitlines() if ln.strip()]
        ends = [ln for ln in lines if ln.get("extra", {}).get("event") == "operation.end"]
        assert ends, "expected an operation.end line"
        end = ends[-1]
        assert end["extra"]["tool"] == "list_models"
        assert end["extra"]["outcome"] == "ok"
        assert end["extra"]["correlation_id"]
        assert "duration_ms" in end["extra"]
        # The adapter name comes from the type of the adapter instance.
        assert end["extra"]["adapter"] == "_CountingAdapter"
        logging.shutdown()

    def test_error_outcome_is_category(self, capfd: pytest.CaptureFixture[str]) -> None:
        configure_logging("INFO", force=True)
        service, adapter = _service_with_logging()
        adapter.next_error = ValidationError("bad input")
        adapter.error_budget = 1
        with pytest.raises(ValidationError):
            service.create_instance("host", {"x": 1})
        sys.stderr.flush()
        out = capfd.readouterr()
        lines = [json.loads(ln) for ln in out.err.splitlines() if ln.strip()]
        ends = [ln for ln in lines if ln.get("extra", {}).get("event") == "operation.end"]
        assert ends[-1]["extra"]["outcome"] == "validation"
        logging.shutdown()

    def test_correlation_id_is_stable_within_one_operation(
        self, capfd: pytest.CaptureFixture[str]
    ) -> None:
        configure_logging("INFO", force=True)
        service, _ = _service_with_logging()
        service.get_instances("host", page_size=10)
        sys.stderr.flush()
        out = capfd.readouterr()
        lines = [json.loads(ln) for ln in out.err.splitlines() if ln.strip()]
        ops = [ln for ln in lines if "event" in ln.get("extra", {})]
        cids = {ln["extra"]["correlation_id"] for ln in ops}
        assert len(cids) == 1  # start and end share one ID
        logging.shutdown()


# ---------------------------------------------------------------------------
# Bounded retry policy
# ---------------------------------------------------------------------------
class TestRetry:
    def test_idempotent_success_after_two_timeouts(self) -> None:
        calls = 0

        def fn() -> str:
            nonlocal calls
            calls += 1
            if calls <= 2:
                raise CoreTimeoutError("upstream timed out")
            return "ok"

        result = retry(
            fn,
            is_idempotent=True,
            max_retries=3,
            sleep=lambda _s: None,
        )
        assert result == "ok"
        assert calls == 3

    def test_write_operation_not_retried(self) -> None:
        calls = 0

        def fn() -> None:
            nonlocal calls
            calls += 1
            raise CoreTimeoutError("boom")

        with pytest.raises(CoreTimeoutError):
            retry(fn, is_idempotent=False, max_retries=5, sleep=lambda _s: None)
        assert calls == 1

    def test_non_retryable_error_not_retried(self) -> None:
        calls = 0

        def fn() -> None:
            nonlocal calls
            calls += 1
            raise ValidationError("bad")

        with pytest.raises(ValidationError):
            retry(fn, is_idempotent=True, max_retries=5, sleep=lambda _s: None)
        assert calls == 1

    def test_not_found_error_not_retried(self) -> None:
        """A 404 (not_found) is not transient; the retry budget must not be spent."""
        calls = 0

        def fn() -> None:
            nonlocal calls
            calls += 1
            raise NotFoundError("no such resource")

        with pytest.raises(NotFoundError):
            retry(fn, is_idempotent=True, max_retries=5, sleep=lambda _s: None)
        assert calls == 1

    def test_exhaustion_preserves_category(self) -> None:
        calls = 0

        def fn() -> None:
            nonlocal calls
            calls += 1
            raise RateLimitError("slow down")

        with pytest.raises(RateLimitError) as exc:
            retry(
                fn,
                is_idempotent=True,
                max_retries=2,
                sleep=lambda _s: None,
            )
        assert calls == 3  # 1 initial + 2 retries
        assert exc.value.category.value == "rate_limit"

    def test_upstream_server_error_is_retryable(self) -> None:
        calls = 0

        def fn() -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise UpstreamServerError("500")
            return "recovered"

        assert (
            retry(fn, is_idempotent=True, max_retries=1, sleep=lambda _s: None)
            == "recovered"
        )
        assert calls == 2

    def test_backoff_delay_is_bounded(self) -> None:
        from cmdb_mcp_adapter.observability.retry import _backoff_delay

        delays = [_backoff_delay(10, 0.5, 30.0) for _ in range(50)]
        assert all(0 <= d <= 30.0 for d in delays)
        # With a large attempt the exponential is capped at 30.
        assert all(d <= 30.0 for d in delays)

    def test_sleep_is_invoked_with_delay(self) -> None:
        sleeps: list[float] = []
        calls = 0

        def fn() -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise CoreTimeoutError("t")
            return "ok"

        retry(
            fn,
            is_idempotent=True,
            max_retries=2,
            backoff_base_seconds=0.01,
            backoff_cap_seconds=0.1,
            sleep=lambda s: sleeps.append(s),
        )
        assert len(sleeps) == 1
        assert 0 <= sleeps[0] <= 0.1
