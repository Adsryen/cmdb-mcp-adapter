"""Core contract tests: drive ``CoreService`` through a ``MockAdapter`` and
assert the normalized behavior of the seven operations across success, empty,
pagination, validation, unsupported, and upstream-error scenarios.

These tests never touch the network.  The mock adapter is deterministic and
can be configured to inject errors or disable specific operations.
"""

from __future__ import annotations

import pytest

from cmdb_mcp_adapter.adapters.mock import MockAdapter
from cmdb_mcp_adapter.core import (
    AuthenticationError,
    CMDBInstance,
    CMDBModel,
    CMDBRelation,
    CoreError,
    CoreService,
    ErrorCategory,
    NotFoundError,
    Page,
    RateLimitError,
    TimeoutError,
    UnsupportedOperationError,
    UpstreamServerError,
    ValidationError,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_adapter() -> MockAdapter:
    return MockAdapter()


@pytest.fixture
def service(mock_adapter: MockAdapter) -> CoreService:
    return CoreService(mock_adapter)


# ---------------------------------------------------------------------------
# get_models
# ---------------------------------------------------------------------------


class TestGetModels:
    def test_returns_all_models(self, service: CoreService) -> None:
        models = service.list_models()
        assert len(models) == 2
        assert models[0].name == "host"
        assert models[1].name == "service"

    def test_models_are_instances_of_CMDBModel(self, service: CoreService) -> None:
        models = service.list_models()
        for m in models:
            assert isinstance(m, CMDBModel)

    def test_model_fields_are_normalized(self, service: CoreService) -> None:
        models = {m.name: m for m in service.list_models()}
        host = models["host"]
        field_names = [f.name for f in host.fields]
        assert "hostname" in field_names
        assert "ip" in field_names

    def test_empty_when_adapter_returns_empty(self) -> None:
        from typing import Any

        class EmptyAdapter:
            def list_models(self) -> list[CMDBModel]:
                return []

            def get_instances(
                self,
                model_name: str,
                *,
                page_size: int,
                cursor: str | None,
                filters: dict[str, Any],
            ) -> Page[CMDBInstance]:
                raise UnsupportedOperationError("nope")

            def create_instance(
                self, model_name: str, data: dict[str, Any]
            ) -> CMDBInstance:
                raise UnsupportedOperationError("nope")

            def update_instance(
                self,
                model_name: str,
                instance_id: str,
                data: dict[str, Any],
            ) -> CMDBInstance:
                raise UnsupportedOperationError("nope")

            def delete_instance(self, model_name: str, instance_id: str) -> None:
                raise UnsupportedOperationError("nope")

            def search_instances(
                self,
                model_name: str,
                query: str,
                *,
                page_size: int,
                cursor: str | None,
            ) -> Page[CMDBInstance]:
                raise UnsupportedOperationError("nope")

            def get_relations(
                self, model_name: str, instance_id: str
            ) -> list[CMDBRelation]:
                raise UnsupportedOperationError("nope")

        svc = CoreService(EmptyAdapter())
        assert svc.list_models() == []


# ---------------------------------------------------------------------------
# get_instances (pagination)
# ---------------------------------------------------------------------------


class TestGetInstances:
    def test_first_page(self, service: CoreService) -> None:
        page = service.get_instances("host", page_size=3)
        assert isinstance(page, Page)
        assert page.page == 1
        assert page.page_size == 3
        assert page.total == 8
        assert len(page.items) == 3
        assert page.items[0].instance_id == "host-1"
        assert page.has_next is True
        assert page.next_cursor is not None

    def test_second_page(self, service: CoreService) -> None:
        page1 = service.get_instances("host", page_size=3)
        page2 = service.get_instances(
            "host", page_size=3, cursor=page1.next_cursor
        )
        assert page2.page == 2
        assert [i.instance_id for i in page2.items] == [
            "host-4",
            "host-5",
            "host-6",
        ]
        assert page2.has_next is True

    def test_last_page_has_no_next(self, service: CoreService) -> None:
        page1 = service.get_instances("host", page_size=3)
        page2 = service.get_instances("host", page_size=3, cursor=page1.next_cursor)
        page3 = service.get_instances("host", page_size=3, cursor=page2.next_cursor)
        assert page3.page == 3
        assert [i.instance_id for i in page3.items] == ["host-7", "host-8"]
        assert page3.has_next is False
        assert page3.next_cursor is None

    def test_page_size_larger_than_total(self, service: CoreService) -> None:
        page = service.get_instances("host", page_size=100)
        assert len(page.items) == 8
        assert page.has_next is False
        assert page.next_cursor is None

    def test_beyond_end_cursor_returns_empty(self, service: CoreService) -> None:
        # A cursor past the end yields an empty page, not an error.
        page = service.get_instances("host", page_size=3, cursor="9")
        assert page.items == []
        assert page.has_next is False
        assert page.next_cursor is None

    def test_filters(self, service: CoreService) -> None:
        page = service.get_instances(
            "host", page_size=100, filters={"role": "web"}
        )
        assert page.total == 8  # all mock hosts have role=web
        assert all(i.data.get("role") == "web" for i in page.items)

    def test_empty_result_for_no_matches(self, service: CoreService) -> None:
        page = service.get_instances(
            "host", page_size=100, filters={"role": "nonexistent"}
        )
        assert page.items == []
        assert page.total == 0

    def test_unknown_model_raises_not_found(self, service: CoreService) -> None:
        with pytest.raises(NotFoundError):
            service.get_instances("nonexistent", page_size=10)

    def test_instances_are_normalized(self, service: CoreService) -> None:
        page = service.get_instances("host", page_size=1)
        inst = page.items[0]
        assert isinstance(inst, CMDBInstance)
        assert inst.model_name == "host"
        assert isinstance(inst.data, dict)


# ---------------------------------------------------------------------------
# create / update / delete
# ---------------------------------------------------------------------------


class TestMutations:
    def test_create(self, service: CoreService) -> None:
        inst = service.create_instance("service", {"name": "x", "env": "dev"})
        assert isinstance(inst, CMDBInstance)
        assert inst.model_name == "service"
        assert inst.data == {"name": "x", "env": "dev"}
        assert inst.instance_id.startswith("service-new-")

    def test_update(self, service: CoreService) -> None:
        inst = service.create_instance("service", {"name": "x"})
        updated = service.update_instance(
            "service", inst.instance_id, {"env": "prod"}
        )
        assert updated.data["name"] == "x"
        assert updated.data["env"] == "prod"

    def test_delete(self, service: CoreService) -> None:
        inst = service.create_instance("service", {"name": "x"})
        service.delete_instance("service", inst.instance_id)
        # After delete, the instance is gone from the adapter.
        page = service.get_instances("service", page_size=100)
        assert all(i.instance_id != inst.instance_id for i in page.items)

    def test_delete_nonexistent_raises_not_found(self, service: CoreService) -> None:
        with pytest.raises(NotFoundError):
            service.delete_instance("service", "does-not-exist")


# ---------------------------------------------------------------------------
# search_instances
# ---------------------------------------------------------------------------


class TestSearch:
    def test_search_returns_matches(self, service: CoreService) -> None:
        page = service.search_instances("host", "web-1", page_size=100)
        assert page.total >= 1
        assert all("web-1" in str(i.data.get("hostname", "")) for i in page.items)

    def test_search_no_matches(self, service: CoreService) -> None:
        page = service.search_instances("host", "zzz-nonexistent", page_size=100)
        assert page.items == []

    def test_search_pagination(self, service: CoreService) -> None:
        page1 = service.search_instances("host", "web", page_size=3)
        assert len(page1.items) == 3
        assert page1.has_next is True
        page2 = service.search_instances(
            "host", "web", page_size=3, cursor=page1.next_cursor
        )
        assert len(page2.items) == 3


# ---------------------------------------------------------------------------
# get_relations
# ---------------------------------------------------------------------------


class TestRelations:
    def test_get_relations(self, service: CoreService) -> None:
        relations = service.get_relations("host", "host-1")
        assert len(relations) == 1
        assert relations[0].source_instance_id == "host-1"
        assert relations[0].target_instance_id == "svc-orders"
        assert isinstance(relations[0], CMDBRelation)

    def test_get_relations_empty(self, service: CoreService) -> None:
        relations = service.get_relations("host", "host-99")
        assert relations == []


# ---------------------------------------------------------------------------
# Input validation (all raise ValidationError)
# ---------------------------------------------------------------------------


class TestValidation:
    def test_empty_model_name_get_instances(self, service: CoreService) -> None:
        with pytest.raises(ValidationError) as exc:
            service.get_instances("", page_size=10)
        assert "model_name" in str(exc.value)

    def test_empty_model_name_create(self, service: CoreService) -> None:
        with pytest.raises(ValidationError):
            service.create_instance("", {"x": 1})

    def test_empty_model_name_update(self, service: CoreService) -> None:
        with pytest.raises(ValidationError):
            service.update_instance("", "id", {"x": 1})

    def test_empty_model_name_delete(self, service: CoreService) -> None:
        with pytest.raises(ValidationError):
            service.delete_instance("", "id")

    def test_empty_model_name_search(self, service: CoreService) -> None:
        with pytest.raises(ValidationError):
            service.search_instances("", "q", page_size=10)

    def test_empty_model_name_relations(self, service: CoreService) -> None:
        with pytest.raises(ValidationError):
            service.get_relations("", "id")

    def test_empty_instance_id_update(self, service: CoreService) -> None:
        with pytest.raises(ValidationError):
            service.update_instance("host", "", {"x": 1})

    def test_empty_instance_id_delete(self, service: CoreService) -> None:
        with pytest.raises(ValidationError):
            service.delete_instance("host", "")

    def test_empty_instance_id_relations(self, service: CoreService) -> None:
        with pytest.raises(ValidationError):
            service.get_relations("host", "")

    def test_zero_page_size(self, service: CoreService) -> None:
        with pytest.raises(ValidationError):
            service.get_instances("host", page_size=0)

    def test_negative_page_size(self, service: CoreService) -> None:
        with pytest.raises(ValidationError):
            service.get_instances("host", page_size=-1)

    def test_non_dict_data_create(self, service: CoreService) -> None:
        with pytest.raises(ValidationError):
            service.create_instance("host", "not-a-dict")  # type: ignore[arg-type]

    def test_non_dict_data_update(self, service: CoreService) -> None:
        with pytest.raises(ValidationError):
            service.update_instance("host", "id", "not-a-dict")  # type: ignore[arg-type]

    def test_non_dict_filters(self, service: CoreService) -> None:
        with pytest.raises(ValidationError):
            service.get_instances("host", page_size=10, filters="bad")  # type: ignore[arg-type]

    def test_non_string_query(self, service: CoreService) -> None:
        with pytest.raises(ValidationError):
            service.search_instances("host", 123, page_size=10)  # type: ignore[arg-type]

    def test_page_size_is_clamped(self, service: CoreService) -> None:
        # A huge page_size is clamped to MAX_PAGE_SIZE, not rejected.
        from cmdb_mcp_adapter.core import MAX_PAGE_SIZE

        page = service.get_instances("host", page_size=MAX_PAGE_SIZE + 100)
        assert page.page_size is not None
        assert page.page_size <= MAX_PAGE_SIZE


# ---------------------------------------------------------------------------
# Unsupported operations
# ---------------------------------------------------------------------------


class TestUnsupported:
    def test_unsupported_operation(self) -> None:
        adapter = MockAdapter(unsupported={"get_relations": "no relations"})
        svc = CoreService(adapter)
        with pytest.raises(UnsupportedOperationError):
            svc.get_relations("host", "host-1")

    def test_unsupported_all(self) -> None:
        adapter = MockAdapter(unsupported={"*": "disabled"})
        svc = CoreService(adapter)
        with pytest.raises(UnsupportedOperationError):
            svc.list_models()
        with pytest.raises(UnsupportedOperationError):
            svc.get_instances("host", page_size=10)


# ---------------------------------------------------------------------------
# Upstream error mapping (category is preserved)
# ---------------------------------------------------------------------------


class TestUpstreamErrors:
    @pytest.mark.parametrize(
        "err_factory,expected_category",
        [
            (lambda: AuthenticationError("bad creds"), ErrorCategory.AUTHENTICATION),
            (lambda: NotFoundError("gone"), ErrorCategory.NOT_FOUND),
            (lambda: TimeoutError("slow"), ErrorCategory.TIMEOUT),
            (lambda: RateLimitError("429"), ErrorCategory.RATE_LIMIT),
            (lambda: UpstreamServerError("500"), ErrorCategory.UPSTREAM_SERVER),
        ],
        ids=["auth", "notfound", "timeout", "ratelimit", "upstream500"],
    )
    def test_category_preserved(
        self,
        err_factory: callable,
        expected_category: ErrorCategory,
    ) -> None:
        err: CoreError = err_factory()  # type: ignore[assignment]
        adapter = MockAdapter(error_injection={"*": err})
        svc = CoreService(adapter)
        with pytest.raises(type(err)) as exc_info:
            svc.list_models()
        assert exc_info.value.category == expected_category

    def test_upstream_detail_not_in_message(self) -> None:
        err = RateLimitError("throttled", upstream_detail="RAW-SECRET-TOKEN")
        adapter = MockAdapter(error_injection={"*": err})
        svc = CoreService(adapter)
        with pytest.raises(RateLimitError) as exc_info:
            svc.list_models()
        # The describe() method (used by the MCP layer) must not leak detail.
        assert "RAW-SECRET-TOKEN" not in exc_info.value.describe()
        # But the upstream_detail attribute is preserved for internal logging.
        assert exc_info.value.upstream_detail == "RAW-SECRET-TOKEN"

    def test_generic_exception_becomes_internal(self) -> None:
        from typing import Any

        class BoomAdapter:
            def list_models(self) -> list[CMDBModel]:
                raise ValueError("boom")

            def get_instances(
                self,
                model_name: str,
                *,
                page_size: int,
                cursor: str | None,
                filters: dict[str, Any],
            ) -> Page[CMDBInstance]:
                raise ValueError("boom")

            def create_instance(
                self, model_name: str, data: dict[str, Any]
            ) -> CMDBInstance:
                raise ValueError("boom")

            def update_instance(
                self,
                model_name: str,
                instance_id: str,
                data: dict[str, Any],
            ) -> CMDBInstance:
                raise ValueError("boom")

            def delete_instance(self, model_name: str, instance_id: str) -> None:
                raise ValueError("boom")

            def search_instances(
                self,
                model_name: str,
                query: str,
                *,
                page_size: int,
                cursor: str | None,
            ) -> Page[CMDBInstance]:
                raise ValueError("boom")

            def get_relations(
                self, model_name: str, instance_id: str
            ) -> list[CMDBRelation]:
                raise ValueError("boom")

        from cmdb_mcp_adapter.core import InternalError

        svc = CoreService(BoomAdapter())
        with pytest.raises(InternalError) as exc_info:
            svc.list_models()
        # The error is normalized to a CoreError with an internal category.
        assert exc_info.value.category == ErrorCategory.INTERNAL
