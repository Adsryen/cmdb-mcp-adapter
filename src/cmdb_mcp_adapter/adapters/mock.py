"""In-memory mock CMDB adapter used for contract tests and local development.

The mock implements the full :class:`~cmdb_mcp_adapter.core.adapter.CMDBAdapter`
protocol with a small, deterministic dataset.  It is deliberately *not*
vendored into the core package: it lives under ``adapters`` so that it can
import core freely (the boundary is one-directional: adapters -> core, never
core -> adapters).

The adapter supports optional error injection and per-operation disablement so
that the contract test suite can drive the failure and unsupported paths
without any real network.
"""

from __future__ import annotations

from typing import Any

from cmdb_mcp_adapter.core.adapter import Filters
from cmdb_mcp_adapter.core.errors import CoreError, NotFoundError, UnsupportedOperationError
from cmdb_mcp_adapter.core.models import (
    CMDBInstance,
    CMDBModel,
    CMDBRelation,
    ModelField,
    Page,
)

#: A single injected failure: the category-bearing CoreError to raise, keyed by
#: the adapter method name it should fire on.  A value of ``None`` means
#: "raise on every operation".
_ERROR_KEY = "*"

#: Operations the mock understands; used to validate the disablement key.
_SUPPORTED_OPS = (
    "list_models",
    "get_instances",
    "create_instance",
    "update_instance",
    "delete_instance",
    "search_instances",
    "get_relations",
)


def _default_models() -> list[CMDBModel]:
    """Two small models so the test dataset stays easy to reason about."""
    return [
        CMDBModel(
            name="host",
            display_name="Host",
            description="A compute host.",
            fields=(
                ModelField(name="hostname", type="string", required=True, description="FQDN."),
                ModelField(name="ip", type="string", required=True, description="IPv4 address."),
                ModelField(
                    name="os",
                    type="string",
                    required=False,
                    description="Operating system.",
                ),
                ModelField(
                    name="role",
                    type="string",
                    required=False,
                    description="Host role.",
                    enum_values=("web", "db", "cache"),
                ),
            ),
        ),
        CMDBModel(
            name="service",
            display_name="Service",
            description="A deployed service.",
            fields=(
                ModelField(name="name", type="string", required=True, description="Service name."),
                ModelField(name="env", type="string", required=False, enum_values=("dev", "prod")),
            ),
        ),
    ]


def _default_instances() -> dict[str, list[CMDBInstance]]:
    """Seed data, grouped by model name, ordered by insertion (== page order)."""
    hosts: list[CMDBInstance] = [
        CMDBInstance(
            model_name="host",
            instance_id=f"host-{i}",
            data={
                "hostname": f"web-{i}.example.com",
                "ip": f"10.0.{i // 10}.{i % 10}",
                "os": "linux",
                "role": "web",
            },
        )
        for i in range(1, 9)
    ]
    services: list[CMDBInstance] = [
        CMDBInstance(
            model_name="service",
            instance_id="svc-orders",
            data={"name": "orders", "env": "prod"},
        ),
        CMDBInstance(
            model_name="service",
            instance_id="svc-billing",
            data={"name": "billing", "env": "prod"},
        ),
    ]
    return {"host": hosts, "service": services}


def _default_relations() -> dict[str, list[CMDBRelation]]:
    return {
        "host-1": [
            CMDBRelation(
                source_instance_id="host-1",
                target_instance_id="svc-orders",
                relation_type="runs",
            ),
        ],
        "host-2": [
            CMDBRelation(
                source_instance_id="host-2",
                target_instance_id="svc-billing",
                relation_type="runs",
            ),
        ],
    }


class MockAdapter:
    """A deterministic in-memory :class:`CMDBAdapter` implementation.

    Arguments:
        error_injection: Optional mapping from an operation name (one of
            :data:`_SUPPORTED_OPS`) or ``"*"`` (all operations) to a
            :class:`CoreError` instance to raise whenever that operation is
            invoked.
        unsupported: Optional mapping from an operation name (or ``"*"``) to
            the message used to build an ``UnsupportedOperationError``.  When
            present for an operation, that operation always fails as
            unsupported.
    """

    def __init__(
        self,
        error_injection: dict[str, CoreError] | None = None,
        unsupported: dict[str, str] | None = None,
    ) -> None:
        self._models = _default_models()
        self._instances = _default_instances()
        self._relations = _default_relations()
        self._error_injection = dict(error_injection or {})
        self._unsupported = dict(unsupported or {})
        # A monotonic id counter for created instances.
        self._next_id = 1

    # -- internal helpers ---------------------------------------------------

    def _check_disabled(self, op: str) -> None:
        if op in self._unsupported or _ERROR_KEY in self._unsupported:
            message = self._unsupported.get(op) or self._unsupported.get(_ERROR_KEY, "")
            raise UnsupportedOperationError(
                f"operation '{op}' is not supported by this adapter: {message}".strip()
            )
        if op in self._error_injection or _ERROR_KEY in self._error_injection:
            err = self._error_injection.get(op)
            if err is None:
                err = self._error_injection.get(_ERROR_KEY)
            assert err is not None
            raise err

    @staticmethod
    def _page(total_items: list[Any], page_size: int, cursor: str | None) -> Page[Any]:
        """Slice an ordered list into a cursor-paginated Page.

        The cursor is a base-36-encoded 1-based page number so it is a stable,
        opaque string a client can echo back.
        """
        page_no = 1 if cursor is None else int(cursor, 36)
        start = (page_no - 1) * page_size
        chunk = total_items[start : start + page_size]
        next_start = start + page_size
        # The next cursor is the 1-based page number of the following page,
        # hex-encoded so it is an opaque, compact string.  It is only
        # non-None when there is at least one item past ``next_start``.
        next_cursor = (
            f"{page_no + 1:x}" if next_start < len(total_items) else None
        )
        return Page(
            items=chunk,
            page=page_no,
            page_size=page_size,
            total=len(total_items),
            next_cursor=next_cursor,
        )

    def _model_or_raise(self, model_name: str) -> None:
        if model_name not in self._instances:
            raise NotFoundError(f"model '{model_name}' does not exist")

    # -- CMDBAdapter protocol ----------------------------------------------

    def list_models(self) -> list[CMDBModel]:
        self._check_disabled("list_models")
        return list(self._models)

    def get_instances(
        self,
        model_name: str,
        *,
        page_size: int,
        cursor: str | None,
        filters: Filters,
    ) -> Page[CMDBInstance]:
        self._check_disabled("get_instances")
        self._model_or_raise(model_name)
        items = self._instances.get(model_name, [])
        if filters:
            items = [
                inst
                for inst in items
                if all(
                    _matches(inst.data.get(k), v) for k, v in filters.items()
                )
            ]
        return self._page(items, page_size, cursor)

    def create_instance(self, model_name: str, data: dict[str, Any]) -> CMDBInstance:
        self._check_disabled("create_instance")
        self._model_or_raise(model_name)
        instance_id = f"{model_name}-new-{self._next_id}"
        self._next_id += 1
        instance = CMDBInstance(
            model_name=model_name,
            instance_id=instance_id,
            data=dict(data),
        )
        self._instances[model_name].append(instance)
        return instance

    def update_instance(
        self,
        model_name: str,
        instance_id: str,
        data: dict[str, Any],
    ) -> CMDBInstance:
        self._check_disabled("update_instance")
        self._model_or_raise(model_name)
        for i, inst in enumerate(self._instances[model_name]):
            if inst.instance_id == instance_id:
                merged = {**inst.data, **data}
                updated = CMDBInstance(
                    model_name=inst.model_name,
                    instance_id=inst.instance_id,
                    data=merged,
                    metadata=inst.metadata,
                )
                self._instances[model_name][i] = updated
                return updated
        raise NotFoundError(f"instance '{instance_id}' not found in model '{model_name}'")

    def delete_instance(self, model_name: str, instance_id: str) -> None:
        self._check_disabled("delete_instance")
        self._model_or_raise(model_name)
        instances = self._instances[model_name]
        for i, inst in enumerate(instances):
            if inst.instance_id == instance_id:
                del instances[i]
                return
        raise NotFoundError(f"instance '{instance_id}' not found in model '{model_name}'")

    def search_instances(
        self,
        model_name: str,
        query: str,
        *,
        page_size: int,
        cursor: str | None,
    ) -> Page[CMDBInstance]:
        self._check_disabled("search_instances")
        self._model_or_raise(model_name)
        needle = query.lower()
        matches = [
            inst
            for inst in self._instances.get(model_name, [])
            if any(needle in str(v).lower() for v in inst.data.values())
        ]
        return self._page(matches, page_size, cursor)

    def get_relations(self, model_name: str, instance_id: str) -> list[CMDBRelation]:
        self._check_disabled("get_relations")
        self._model_or_raise(model_name)
        return list(self._relations.get(instance_id, []))


def _matches(value: Any, expected: Any) -> bool:
    """Match a stored field value against a filter value.

    A list-valued filter is a multi-value equality (value in list).  A scalar
    filter is a case-insensitive string equality for strings and direct
    equality otherwise.
    """
    if isinstance(expected, list):
        return any(_matches(value, item) for item in expected)
    if isinstance(value, str) and isinstance(expected, str):
        return value.lower() == expected.lower()
    return bool(value == expected)
