"""Pure-mapping tests for :mod:`cmdb_mcp_adapter.adapters.veops.mapper`.

Covers the *verified* VeOps 2.6.2 payload shapes (list-wrapped
``ci_types``, flat ``cis``/``result``/``second_cis`` instance dicts,
global attribute dictionary, bare ``relation_types`` array) and the
vendor-metadata split into ``metadata`` under the ``veops.`` prefix.
"""

from __future__ import annotations

import pytest

from cmdb_mcp_adapter.adapters.veops import mapper


def _flat_ci() -> dict:
    return {
        "_id": 514,
        "_type": 1,
        "_updated_at": "2026-08-07 18:04:42",
        "_updated_by": "admin",
        "ci_type": "quick_reference",
        "ci_type_alias": "快速查询",
        "unique": "quick_re_source_key",
        "unique_alias": "导入唯一键",
        "quick_re_title": "用途",
        "quick_re_content": "不复制资产明细。",
        "quick_re_source_key": "00-快速查询-ROW-0002",
    }


class TestModelMapping:
    def test_ci_type_to_model(self) -> None:
        raw = {
            "id": 7,
            "name": "credential",
            "alias": "配置与凭据",
            "unique_key": "credenti_key_id",
            "enabled": True,
        }
        m = mapper.map_ci_type(raw, fields=())
        assert m.name == "credential"
        assert m.display_name == "配置与凭据"
        assert m.fields == ()

    def test_ci_types_list_wrapper(self) -> None:
        payload = {
            "numfound": 2,
            "ci_types": [
                {"id": 1, "name": "quick_reference", "alias": "快速查询"},
                {"id": 7, "name": "credential", "alias": "配置与凭据"},
            ],
        }
        models = mapper.map_ci_types(payload)
        assert [m.name for m in models] == ["quick_reference", "credential"]
        assert models[1].display_name == "配置与凭据"

    def test_ci_types_with_fields_by_name(self) -> None:
        field = mapper.map_attribute({"name": "credenti_key_id", "value_type": "2"})
        assert field is not None
        fields_by_name = {"credential": (field,)}
        payload = {
            "ci_types": [
                {"id": 7, "name": "credential"},
                {"id": 9, "name": "no_match"},
            ]
        }
        models = mapper.map_ci_types(payload, fields_by_name)
        assert [f.name for f in models[0].fields] == ["credenti_key_id"]
        assert models[1].fields == ()

    def test_empty_ci_types(self) -> None:
        assert mapper.map_ci_types({}) == []
        assert mapper.map_ci_types({"ci_types": None}) == []

    def test_attribute_mapping(self) -> None:
        field = mapper.map_attribute(
            {
                "id": 1,
                "name": "quick_re_title",
                "alias": "快速查询与联查",
                "value_type": "2",
                "is_choice": False,
            }
        )
        assert field is not None
        assert field.name == "quick_re_title"
        assert field.type == "string"
        assert field.required is False
        assert field.description == "快速查询与联查"
        assert field.enum_values is None

    def test_choice_attribute_becomes_enum(self) -> None:
        field = mapper.map_attribute(
            {
                "id": 2,
                "name": "status",
                "alias": "状态",
                "value_type": "2",
                "is_choice": True,
                "choice": "启用,停用,已删除",
            }
        )
        assert field is not None
        assert field.enum_values == ("启用", "停用", "已删除")

    def test_value_type_codes(self) -> None:
        assert mapper._value_type_to_core("2") == "string"
        assert mapper._value_type_to_core("1") == "int"
        assert mapper._value_type_to_core("unknown") == "string"

    def test_map_attributes(self) -> None:
        payload = {
            "attributes": [
                {"name": "a", "value_type": "2"},
                {"name": "b", "value_type": "1"},
                {"name": None},
                "not-a-dict",
            ]
        }
        fields = mapper.map_attributes(payload)
        assert [f.name for f in fields] == ["a", "b"]


class TestInstanceMapping:
    def test_flat_ci_split(self) -> None:
        inst = mapper.map_ci(_flat_ci(), model_name="quick_reference", known_fields=())
        assert inst.instance_id == "514"
        assert inst.model_name == "quick_reference"
        # Attribute values land in data.
        assert inst.data["quick_re_title"] == "用途"
        assert inst.data["quick_re_content"] == "不复制资产明细。"
        # Vendor metadata keys are quarantined under the prefix.
        assert inst.metadata is not None
        assert inst.metadata["veops._type"] == 1
        assert inst.metadata["veops._updated_at"] == "2026-08-07 18:04:42"
        assert inst.metadata["veops._updated_by"] == "admin"
        assert inst.metadata["veops.ci_type"] == "quick_reference"
        assert inst.metadata["veops.ci_type_alias"] == "快速查询"
        assert inst.metadata["veops.unique"] == "quick_re_source_key"
        assert inst.metadata["veops.unique_alias"] == "导入唯一键"
        # The native id is always preserved.
        assert inst.metadata["veops.id"] == "514"

    def test_known_fields_quarantine_unknown(self) -> None:
        inst = mapper.map_ci(
            _flat_ci(), model_name="quick_reference", known_fields=("quick_re_title",)
        )
        assert inst.data == {"quick_re_title": "用途"}
        # Non-declared attribute values are quarantined, not dropped.
        assert inst.metadata["veops.quick_re_content"] == "不复制资产明细。"
        assert inst.metadata["veops.quick_re_source_key"] == "00-快速查询-ROW-0002"

    def test_cis_payload(self) -> None:
        payload = {"cis": [_flat_ci(), {"_id": 515, "quick_re_title": "t2"}]}
        insts = mapper.map_cis(payload, model_name="quick_reference")
        assert [i.instance_id for i in insts] == ["514", "515"]

    def test_empty_cis(self) -> None:
        assert mapper.map_cis({}, model_name="x") == []

    def test_search_result_payload(self) -> None:
        payload = {"result": [_flat_ci()]}
        insts = mapper.map_search_result(payload, model_name="quick_reference")
        assert insts[0].instance_id == "514"


class TestRelationMapping:
    def test_second_cis(self) -> None:
        payload = {
            "numfound": 1,
            "total": 1,
            "page": 1,
            "second_cis": [
                {
                    "_id": 1,
                    "_type": 3,
                    "ci_type": "server",
                    "ci_type_alias": "服务器",
                    "server_credential_id": "ACC-0166",
                }
            ],
        }
        rels = mapper.map_second_cis(
            payload, source_instance_id="731", relation_type=""
        )
        assert len(rels) == 1
        assert rels[0].source_instance_id == "731"
        assert rels[0].target_instance_id == "1"
        assert rels[0].relation_type == ""
        assert rels[0].metadata is not None
        assert rels[0].metadata["veops.ci_type"] == "server"
        assert rels[0].metadata["veops.server_credential_id"] == "ACC-0166"

    def test_first_cis(self) -> None:
        payload = {
            "numfound": 2,
            "first_cis": [{"_id": 10, "ci_type": "a"}, {"_id": 11, "ci_type": "b"}],
        }
        rels = mapper.map_first_cis(
            payload, target_instance_id="99", relation_type="承载"
        )
        assert [r.source_instance_id for r in rels] == ["10", "11"]
        assert all(r.target_instance_id == "99" for r in rels)
        assert rels[0].relation_type == "承载"

    def test_empty_relations(self) -> None:
        assert mapper.map_second_cis({}, source_instance_id="1") == []

    def test_relation_types_bare_array(self) -> None:
        payload = [
            {"id": 1, "name": "承载"},
            {"id": 2, "name": "关联凭据"},
            {"name": "no-id"},
            "junk",
        ]
        out = mapper.map_relation_types(payload)
        assert out == {1: "承载", 2: "关联凭据"}
        assert mapper.map_relation_types({"a": 1}) == {}


class TestCursorEncoding:
    def test_none_is_page_one(self) -> None:
        assert mapper.decode_search_cursor(None) == 1

    def test_round_trip(self) -> None:
        for page in (1, 2, 42, 1000):
            assert mapper.decode_search_cursor(mapper.encode_search_cursor(page)) == page

    def test_cursor_is_opaque_string(self) -> None:
        assert mapper.encode_search_cursor(3) == "veops-search-page-3"

    def test_bad_cursor_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            mapper.decode_search_cursor("not-a-cursor")
        with pytest.raises(ValueError):
            mapper.decode_search_cursor("veops-search-page-0")
        with pytest.raises(ValueError):
            mapper.decode_search_cursor("veops-search-page-abc")
        # The old-style instance cursor is no longer accepted anywhere.
        with pytest.raises(ValueError):
            mapper.decode_search_cursor("veops-page-2")
