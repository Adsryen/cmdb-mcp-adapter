"""The core service: input validation plus delegation to a CMDB adapter.

The MCP tool layer talks *only* to :class:`CoreService`, never to an adapter
directly.  That keeps the validation rules in exactly one place and lets the
tool layer stay a thin, transport-agnostic translation layer.

Validation performed here is *contract* validation: it checks that the shape
and types of the incoming arguments satisfy the unified contract (non-empty
names, positive page sizes, dict payloads, ...).  Field-level / business
validation (for example "this field is required by the model") is the
adapter's job and is surfaced as the same :class:`ValidationError` category.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from cmdb_mcp_adapter.core.adapter import CMDBAdapter, Filters
from cmdb_mcp_adapter.core.errors import CoreError, ValidationError, normalize
from cmdb_mcp_adapter.core.models import MAX_PAGE_SIZE, CMDBInstance, CMDBModel, CMDBRelation, Page
from cmdb_mcp_adapter.observability.correlation import log_operation

T = TypeVar("T")

#: Smallest page size the core will accept.
MIN_PAGE_SIZE = 1


def _require_non_empty(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} must be a non-empty string")
    return value


def _require_dict(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError(f"{field} must be a mapping/object")
    return value


def _validate_page_size(page_size: object, field: str) -> int:
    # ``bool`` is a subclass of ``int``; reject it explicitly.
    if isinstance(page_size, bool) or not isinstance(page_size, int):
        raise ValidationError(f"{field} must be an integer")
    if page_size < MIN_PAGE_SIZE:
        raise ValidationError(f"{field} must be >= {MIN_PAGE_SIZE}")
    # Clamp to the contract maximum so a hostile page_size cannot force an
    # unbounded response; the adapter still receives an in-contract value.
    return min(page_size, MAX_PAGE_SIZE)


class CoreService:
    """Validates tool input and delegates to the underlying adapter.

    The service is stateless with respect to the CMDB; it simply holds a
    reference to the adapter it was composed with.

    Each public method wraps the adapter call in a correlation-ID /
    structured-log boundary (see :mod:`cmdb_mcp_adapter.observability`).
    The ``tool`` name passed to :func:`log_operation` comes from the MCP
    tool layer (the public contract name); the core falls back to the
    method name when it is not supplied.  No tool input or output values
    are logged — only metadata (correlation ID, tool, adapter, duration,
    outcome) — so a secret in a tool argument can never leak into a log
    line.
    """

    def __init__(self, adapter: CMDBAdapter) -> None:
        self._adapter = adapter

    @property
    def adapter(self) -> CMDBAdapter:
        """The adapter this service delegates to (exposed for tests/diagnostics)."""
        return self._adapter

    def list_models(self) -> list[CMDBModel]:
        """Return the model metadata, propagating adapter errors unchanged."""
        return log_operation(
            lambda: _guarded(self._adapter.list_models),
            tool="list_models",
            adapter_name=type(self._adapter).__name__,
        )

    def get_instances(
        self,
        model_name: str,
        *,
        page_size: int,
        cursor: str | None = None,
        filters: dict[str, Any] | None = None,
    ) -> Page[CMDBInstance]:
        _require_non_empty(model_name, "model_name")
        size = _validate_page_size(page_size, "page_size")
        norm_filters: Filters = (
            {} if filters is None else _require_dict(filters, "filters")
        )
        return log_operation(
            lambda: _guarded(
                self._adapter.get_instances,
                model_name,
                page_size=size,
                cursor=cursor,
                filters=norm_filters,
            ),
            tool="get_instances",
            adapter_name=type(self._adapter).__name__,
        )

    def create_instance(self, model_name: str, data: dict[str, Any]) -> CMDBInstance:
        _require_non_empty(model_name, "model_name")
        payload = _require_dict(data, "data")
        return log_operation(
            lambda: _guarded(self._adapter.create_instance, model_name, payload),
            tool="create_instance",
            adapter_name=type(self._adapter).__name__,
        )

    def update_instance(
        self,
        model_name: str,
        instance_id: str,
        data: dict[str, Any],
    ) -> CMDBInstance:
        _require_non_empty(model_name, "model_name")
        _require_non_empty(instance_id, "instance_id")
        payload = _require_dict(data, "data")
        return log_operation(
            lambda: _guarded(self._adapter.update_instance, model_name, instance_id, payload),
            tool="update_instance",
            adapter_name=type(self._adapter).__name__,
        )

    def delete_instance(self, model_name: str, instance_id: str) -> None:
        _require_non_empty(model_name, "model_name")
        _require_non_empty(instance_id, "instance_id")
        log_operation(
            lambda: _guarded(self._adapter.delete_instance, model_name, instance_id),
            tool="delete_instance",
            adapter_name=type(self._adapter).__name__,
        )

    def search_instances(
        self,
        model_name: str,
        query: str,
        *,
        page_size: int,
        cursor: str | None = None,
    ) -> Page[CMDBInstance]:
        _require_non_empty(model_name, "model_name")
        if not isinstance(query, str):
            raise ValidationError("query must be a string")
        size = _validate_page_size(page_size, "page_size")
        return log_operation(
            lambda: _guarded(
                self._adapter.search_instances,
                model_name,
                query,
                page_size=size,
                cursor=cursor,
            ),
            tool="search_instances",
            adapter_name=type(self._adapter).__name__,
        )

    def get_relations(self, model_name: str, instance_id: str) -> list[CMDBRelation]:
        _require_non_empty(model_name, "model_name")
        _require_non_empty(instance_id, "instance_id")
        return log_operation(
            lambda: _guarded(self._adapter.get_relations, model_name, instance_id),
            tool="get_relations",
            adapter_name=type(self._adapter).__name__,
        )


def _guarded(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Invoke ``fn`` and normalize any non-core exception into a CoreError.

    CoreError subclasses pass through untouched; anything else is wrapped as an
    ``internal`` error so the tool layer never sees an unclassified exception.

    The generic ``T`` is the *declared* return type of ``fn``; the runtime
    value is returned as-is.  ``mypy --strict`` is satisfied because the
    annotation is concrete (not ``Any``) even though the body treats the
    callable as opaque.
    """
    try:
        result: T = fn(*args, **kwargs)
        return result
    except CoreError:
        raise
    except Exception as error:  # noqa: BLE001 - deliberate catch-all boundary
        raise normalize(error) from error
