"""Offline behaviour tests for the four intent-level write tools.

Every fake answer comes from ``tests/adapters/_fakes.py`` (signature-
verifying MockTransport) — no real HTTP, no DB.  Gate thresholds and
column rosters are read from the vendored rules file; the only literals
here are fixture identifiers (ci ids, the model name 'server').
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from cmdb_mcp_adapter import rules_gate as rg
from cmdb_mcp_adapter.adapters.veops.intent import (
    VERDICT_PASS,
    VERDICT_RECHECK,
    VERDICT_REJECT,
    VeopsIntentOps,
)
from cmdb_mcp_adapter.core.errors import UpstreamServerError

from ._fakes import _RecordingTransport, build_adapter

API = "/api/v0.1"

RULES = rg.load_rules()
TICKET = "imp-pytest-runner-20261115-0000"
SECRET_VALUE = "Sup3r-Secret-Plaintext-Elephant"


# ---------------------------------------------------------------------------
# world building
# ---------------------------------------------------------------------------


def _vocab_value(attr_id: str) -> str:
    entry = RULES["rules"]["vocabulary"]["attrs"].get(attr_id)
    assert isinstance(entry, dict) and entry.get("values"), (
        f"rules vocabulary lost attr {attr_id}"
    )
    return str(entry["values"][0])


def _server_binding_names() -> list[str]:
    names: list[str] = [
        str(b["attr_name"]) for b in rg.four_piece_bindings("server", RULES)
    ]
    unique = rg.unique_key_attr("server", RULES)
    assert unique is not None
    names.insert(0, str(unique))
    # roster also carries the fields the happy payload touches
    names.extend(["server_project", "server_hostname"])
    return names


def _valid_create_fields() -> dict[str, Any]:
    unique = str(rg.unique_key_attr("server", RULES))
    fields: dict[str, Any] = {unique: "SRV-0001", "server_hostname": "node-a"}
    for binding in rg.four_piece_bindings("server", RULES):
        role = str(binding["role"])
        name = str(binding["attr_name"])
        if role == "checked_at":
            fields[name] = "2026-11-15"
        elif binding.get("is_choice"):
            fields[name] = _vocab_value(str(binding["attr_id"]))
        else:
            fields[name] = "pytest-value"
    notes_name = next(
        (
            str(b["attr_name"])
            for b in rg.four_piece_bindings("server", RULES)
            if b["role"] == "notes"
        ),
        None,
    )
    if notes_name is not None:
        raw = json.loads(rg.VENDORED_RULES_PATH.read_text(encoding="utf-8"))
        fields[notes_name] = str(raw["rules"]["note_format"]["example_verbatim"])
    return fields


def _binding_attributes() -> dict[str, Any]:
    return {
        "attributes": [
            {"name": n, "alias": n, "value_type": "2", "is_required": False}
            for n in _server_binding_names()
        ]
    }


def _handlers(extra: dict[tuple[str, str], Any] | None = None) -> dict[tuple[str, str], Any]:
    base: dict[tuple[str, str], Any] = {
        ("GET", f"{API}/ci_types"): {
            "ci_types": [
                {"id": 3, "name": "server"},
                {"id": 7, "name": "credential"},
                {"id": 1, "name": "quick_reference"},
            ]
        },
        ("GET", f"{API}/ci_types/3/attributes"): _binding_attributes(),
        ("GET", f"{API}/ci_types/7/attributes"): {
            "attributes": [
                {
                    "name": "credenti_secret_value",
                    "alias": "配置值或密钥",
                    "value_type": "2",
                    "is_required": False,
                }
            ]
        },
        ("GET", f"{API}/ci_types/1/attributes"): {"attributes": []},
        ("GET", f"{API}/ci/type/3"): {"cis": [], "numfound": 0},
        ("GET", f"{API}/relation_types"): [{"id": 7, "name": "部署于", "deleted": 0}],
    }
    if extra:
        base.update(extra)
    return base


def _history_with(ticket: str, hist_rows: int) -> dict[str, Any]:
    return {
        "records": [
            [
                {"id": 900, "ticket_id": ticket},
                [{"attr_id": str(i), "old": "", "new": "x"} for i in range(hist_rows)],
            ]
        ]
    }


def _ops(
    handlers: dict[tuple[str, str], Any],
    *,
    scripts: dict[tuple[str, str], list[Any]] | None = None,
    default: Any = None,
    readonly: bool = False,
) -> tuple[VeopsIntentOps, _RecordingTransport, Any]:
    transport = _RecordingTransport(handlers, default, scripts=scripts)
    adapter, client = build_adapter(transport)
    adapter._readonly = readonly  # simulate VEOPS_READONLY wiring
    return VeopsIntentOps(client, adapter), transport, adapter


def _methods(transport: _RecordingTransport) -> list[str]:
    return [r.method for r in transport.requests]


def _errs(cell: dict[str, Any]) -> set[str]:
    return {e["code"] for e in cell["errs"]}


def _cell_for(result: dict[str, Any], field: str) -> dict[str, Any]:
    for cell in result.get("cells", []):
        if cell["field"] == field:
            return cell
    raise AssertionError(f"cell '{field}' missing in {result!r}")


# ---------------------------------------------------------------------------
# cmdb_preflight_write — zero write side-effects
# ---------------------------------------------------------------------------


def test_preflight_happy_path_uses_only_reads() -> None:
    ops, transport, _ = _ops(_handlers())
    result = ops.preflight_write("server", _valid_create_fields(), TICKET)
    assert result["verdict"] == VERDICT_PASS
    assert set(_methods(transport)) == {"GET"}, "preflight must never write"
    assert result["vocab_source"].startswith("rules@")
    generated = str(RULES["generated_at_utc"])
    assert generated in result["vocab_source"]
    assert "server_hostname" in {c["field"] for c in result["cells"]}


def test_preflight_update_mode_reads_before_state() -> None:
    ops, transport, _ = _ops(
        _handlers(
            {
                ("GET", f"{API}/ci/555"): {
                    "ci": {"_id": 555, "ci_type": 3, "server_hostname": "old-name"}
                }
            }
        )
    )
    result = ops.preflight_write("server", {"server_hostname": "new-name"}, TICKET, ci_id="555")
    assert result["verdict"] == VERDICT_PASS
    cell = _cell_for(result, "server_hostname")
    assert cell["before_len"] == len("old-name")
    assert cell["before_sha12"] == rg.sha12("old-name")
    assert cell["after_len"] == len("new-name")


def test_preflight_rejects_banned_char_naming_char_and_family() -> None:
    raw = json.loads(rg.VENDORED_RULES_PATH.read_text(encoding="utf-8"))
    ranges = raw["rules"]["banned_symbols_T1"]["ranges"]
    arrow = next(r for r in ranges if str(r["lo"]) == "U+2190")
    fields = _valid_create_fields()
    fields["server_hostname"] = f"a{chr(0x2190)}b"
    ops, transport, _ = _ops(_handlers())
    result = ops.preflight_write("server", fields, TICKET)
    assert result["verdict"] == VERDICT_REJECT
    cell = _cell_for(result, "server_hostname")
    assert "BANNED_SYMBOL" in _errs(cell)
    message = cell["errs"][0]["message"]
    assert "U+2190" in message and str(arrow["name"]) in message


def test_preflight_rejects_missing_ticket_and_unknown_field() -> None:
    ops, _, _ = _ops(_handlers())
    fields = _valid_create_fields()
    result = ops.preflight_write("server", fields, "")
    assert result["verdict"] == VERDICT_REJECT
    assert "TICKET_MISSING" in _errs(_cell_for(result, ""))
    result = ops.preflight_write("server", {"no_such_attr": "x", **fields}, TICKET)
    assert "UNKNOWN_FIELD" in _errs(_cell_for(result, "no_such_attr"))


def test_preflight_ci_not_found_is_reject() -> None:
    ops, _, _ = _ops(_handlers())
    result = ops.preflight_write(
        "server", {"server_hostname": "n"}, TICKET, ci_id="424242"
    )
    assert result["verdict"] == VERDICT_REJECT
    assert "CI_NOT_FOUND" in _errs(_cell_for(result, ""))


# ---------------------------------------------------------------------------
# cmdb_create_card — gates, happy face, failure face
# ---------------------------------------------------------------------------


def _create_handlers(ci_id: int = 555) -> dict[tuple[str, str], Any]:
    fields = _valid_create_fields()
    return _handlers(
        {
            ("POST", f"{API}/ci"): {"ci_id": ci_id},
            ("GET", f"{API}/ci/{ci_id}"): {"ci": {"_id": ci_id, "ci_type": 3, **fields}},
            ("GET", f"{API}/history/records/attribute"): _history_with(
                TICKET, len(fields)
            ),
        }
    )


def test_create_card_happy_path_three_rulers_pass() -> None:
    fields = _valid_create_fields()
    ops, transport, _ = _ops(_create_handlers())
    result = ops.create_card("server", TICKET, fields)
    assert result["verdict"] == VERDICT_PASS
    assert result["ci_id"] == "555"
    assert result["cells_readback"] == f"{len(fields)}/{len(fields)}"
    assert result["ticket_records"] == 1
    post = next(r for r in transport.requests if r.method == "POST")
    body = json.loads(post.content)
    assert body["ticket_id"] == TICKET
    assert body["ci_type"] == 3
    assert body["server_hostname"] == fields["server_hostname"]
    assert "no_attribute_policy" not in body  # post_ci_quirks: ⛔ 传


def test_create_card_gate_reject_sends_no_post() -> None:
    ops, transport, _ = _ops(_create_handlers())
    fields = _valid_create_fields()
    fields["server_hostname"] = f"x{chr(0x26D4)}y"
    result = ops.create_card("server", TICKET, fields)
    assert result["verdict"] == VERDICT_REJECT
    assert result["stage"] == "gate"
    assert "POST" not in _methods(transport)
    cell = _cell_for(result, "server_hostname")
    assert "BANNED_SYMBOL" in _errs(cell)
    assert "U+26D4" in cell["errs"][0]["message"]


def test_create_card_rejects_overlong_ticket_from_rules_limit() -> None:
    ops, transport, _ = _ops(_create_handlers())
    limit = rg.ticket_max_len(RULES)
    bad_ticket = "imp-pytest-" + "t" * (limit + 1 - len("imp-pytest-"))
    result = ops.create_card("server", bad_ticket, _valid_create_fields())
    assert result["verdict"] == VERDICT_REJECT
    assert "POST" not in _methods(transport)
    assert "TICKET_TOO_LONG" in _errs(_cell_for(result, ""))


def test_create_card_rejects_note_over_rules_max() -> None:
    fields = _valid_create_fields()
    notes_name = next(
        str(b["attr_name"])
        for b in rg.four_piece_bindings("server", RULES)
        if b["role"] == "notes"
    )
    fields[notes_name] = "x" * (rg.note_max_len(RULES) + 1)
    ops, transport, _ = _ops(_create_handlers())
    result = ops.create_card("server", TICKET, fields)
    assert result["verdict"] == VERDICT_REJECT
    assert "POST" not in _methods(transport)
    assert "NOTE_TOO_LONG" in _errs(_cell_for(result, notes_name))


def test_create_card_rejects_index_column_over_varchar_cap() -> None:
    roster = sorted(rg.index_text_columns(RULES))
    column = roster[0]
    limit = rg.index_text_limit(RULES)
    handlers = _handlers(
        {
            ("GET", f"{API}/ci_types/1/attributes"): {
                "attributes": [
                    {
                        "name": column,
                        "alias": column,
                        "value_type": "2",
                        "is_required": False,
                    }
                ]
            }
        }
    )
    ops, _, _ = _ops(handlers)
    result = ops.preflight_write("quick_reference", {column: "x" * (limit + 1)}, TICKET)
    assert result["verdict"] == VERDICT_REJECT
    assert "INDEX_TEXT_TOO_LONG" in _errs(_cell_for(result, column))


def test_create_card_rejects_enum_outside_vocabulary_and_echoes_list() -> None:
    fields = _valid_create_fields()
    lifecycle = next(
        str(b["attr_name"])
        for b in rg.four_piece_bindings("server", RULES)
        if b["role"] == "lifecycle"
    )
    fields[lifecycle] = "字典外乱填值"
    ops, transport, _ = _ops(_create_handlers())
    result = ops.create_card("server", TICKET, fields)
    assert result["verdict"] == VERDICT_REJECT
    assert "POST" not in _methods(transport)
    cell = _cell_for(result, lifecycle)
    assert "VOCAB_NOT_REGISTERED" in _errs(cell)
    known = str(
        RULES["rules"]["vocabulary"]["attrs"]["166"]["values"][0]
    )
    assert known in cell["errs"][0]["message"]


def test_create_card_rejects_missing_four_piece_column_by_name() -> None:
    fields = _valid_create_fields()
    dropped = next(
        str(b["attr_name"])
        for b in rg.four_piece_bindings("server", RULES)
        if b["role"] == "evidence_source"
    )
    del fields[dropped]
    ops, transport, _ = _ops(_create_handlers())
    result = ops.create_card("server", TICKET, fields)
    assert result["verdict"] == VERDICT_REJECT
    assert "POST" not in _methods(transport)
    payload_cell = _cell_for(result, "")
    missing = [e for e in payload_cell["errs"] if e["code"] == "FOUR_PIECE_COLUMN_MISSING"]
    assert any(dropped in e["message"] for e in missing)


def test_create_card_never_auto_fills_checked_at() -> None:
    fields = _valid_create_fields()
    checked_at = next(
        str(b["attr_name"])
        for b in rg.four_piece_bindings("server", RULES)
        if b["role"] == "checked_at"
    )
    del fields[checked_at]
    ops, transport, _ = _ops(_create_handlers())
    result = ops.create_card("server", TICKET, fields)
    assert result["verdict"] == VERDICT_REJECT
    assert "POST" not in _methods(transport)
    missing = [
        e
        for e in _cell_for(result, "")["errs"]
        if e["code"] == "FOUR_PIECE_COLUMN_MISSING" and checked_at in e["message"]
    ]
    assert missing, "调用方必须显式给 evidence_checked_at（⛔ 服务器自取当天钟）"


def test_create_card_rejects_ticket_id_in_fields_and_sensitive_write(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ops, transport, _ = _ops(_create_handlers())
    result = ops.create_card(
        "server", TICKET, {"ticket_id": TICKET, "server_hostname": "n"}
    )
    assert result["verdict"] == VERDICT_REJECT
    assert "POST" not in _methods(transport)

    secret_name = str(
        json.loads(rg.VENDORED_RULES_PATH.read_text(encoding="utf-8"))[
            "sensitive_columns"
        ][0]["name"]
    )
    ops2, transport2, _ = _ops(_handlers())
    with caplog.at_level("INFO"):
        result2 = ops2.create_card(
            "credential",
            TICKET,
            {secret_name: SECRET_VALUE},
        )
    assert result2["verdict"] == VERDICT_REJECT
    assert "POST" not in _methods(transport2)
    assert "SENSITIVE_WRITE_FORBIDDEN" in _errs(_cell_for(result2, secret_name))
    assert SECRET_VALUE not in json.dumps(result2, ensure_ascii=False)
    assert SECRET_VALUE not in caplog.text
    masked = _cell_for(result2, secret_name)["after_masked"]
    assert masked == rg.mask_value(secret_name, SECRET_VALUE)


def test_create_card_duplicate_unique_key_is_rejected() -> None:
    fields = _valid_create_fields()
    unique = str(rg.unique_key_attr("server", RULES))
    handlers = _handlers(
        {
            ("GET", f"{API}/ci/type/3"): {
                "cis": [{"_id": 7, unique: fields[unique]}],
                "numfound": 1,
            }
        }
    )
    ops, transport, _ = _ops(handlers)
    result = ops.create_card("server", TICKET, fields)
    assert result["verdict"] == VERDICT_REJECT
    assert "POST" not in _methods(transport)
    cell = _cell_for(result, unique)
    assert "DUPLICATE_CARD" in _errs(cell)


def test_create_card_readback_mismatch_gives_recheck_not_pass() -> None:
    fields = _valid_create_fields()
    handlers = _create_handlers()
    ci_id = 555
    diverged = dict(fields)
    diverged["server_hostname"] = "landed-different"
    handlers[("GET", f"{API}/ci/{ci_id}")] = {
        "ci": {"_id": ci_id, "ci_type": 3, **diverged}
    }
    ops, _, _ = _ops(handlers)
    result = ops.create_card("server", TICKET, fields)
    assert result["verdict"] == VERDICT_RECHECK
    assert result["cells_readback"] != f"{len(fields)}/{len(fields)}"
    assert any("SHA" in r for r in result["recheck_reasons"])


def test_create_card_upstream_500_reverse_lookup_never_advises_replay() -> None:
    fields = _valid_create_fields()
    unique = str(rg.unique_key_attr("server", RULES))
    scripts: dict[tuple[str, str], list[Any]] = {
        ("GET", f"{API}/ci/type/3"): [
            {"cis": [], "numfound": 0},  # preflight dedup: clean
            {"cis": [{"_id": 777, unique: fields[unique]}], "numfound": 1},  # reverse hit
        ],
        ("POST", f"{API}/ci"): [(500, "upstream exploded mid-write")],
    }
    ops, _, _ = _ops(_handlers(), scripts=scripts)
    with pytest.raises(UpstreamServerError) as excinfo:
        ops.create_card("server", TICKET, fields)
    message = str(excinfo.value)
    assert "唯一键反查：命中 ci_id=777" in message
    assert "盲目重放" in message
    for banned_word in ("请重试", "重放本调用即可", "retry the call"):
        assert banned_word not in message


def test_create_card_timeout_also_reverse_looks_up() -> None:
    fields = _valid_create_fields()

    def _timeout(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("connect timed out")

    handlers = _create_handlers()
    handlers[("POST", f"{API}/ci")] = _timeout
    ops, _, _ = _ops(handlers)
    with pytest.raises(UpstreamServerError) as excinfo:
        ops.create_card("server", TICKET, fields)
    assert "唯一键反查" in str(excinfo.value)


# ---------------------------------------------------------------------------
# cmdb_update_card — three rulers incl. the 200-empty-write guard
# ---------------------------------------------------------------------------


def _update_handlers(fields: dict[str, Any], *, hist_rows: int, ticket: str = TICKET) -> dict[
    tuple[str, str], Any
]:
    return _handlers(
        {
            ("GET", f"{API}/ci/555"): {"ci": {"_id": 555, "ci_type": 3, **fields}},
            ("PUT", f"{API}/ci/555"): {"ci_id": 555},
            ("GET", f"{API}/history/records/attribute"): _history_with(ticket, hist_rows),
        }
    )


def test_update_card_happy_pass_and_put_body() -> None:
    fields = {"server_hostname": "new-name", "server_project": "PRJ-A"}
    ops, transport, _ = _ops(_update_handlers(fields, hist_rows=len(fields)))
    result = ops.update_card("server", "555", TICKET, fields)
    assert result["verdict"] == VERDICT_PASS
    assert result["cells_readback"] == f"{len(fields)}/{len(fields)}"
    assert result["ticket_records"] == 1
    assert result["attr_hist_rows"] == len(fields)
    put = next(r for r in transport.requests if r.method == "PUT")
    body = json.loads(put.content)
    assert body["ticket_id"] == TICKET
    assert body["server_hostname"] == "new-name"


def test_update_card_empty_write_history_mismatch_recheck() -> None:
    fields = {"server_hostname": "new-name", "server_project": "PRJ-A"}
    ops, transport, _ = _ops(_update_handlers(fields, hist_rows=0))
    result = ops.update_card("server", "555", TICKET, fields)
    assert put_count(transport) == 1
    assert result["verdict"] == VERDICT_RECHECK
    assert any("空改" in r for r in result["recheck_reasons"])


def put_count(transport: _RecordingTransport) -> int:
    return sum(1 for r in transport.requests if r.method == "PUT")


def test_update_card_history_unreachable_rechecks_not_passes() -> None:
    fields = {"server_hostname": "new-name"}
    handlers = _update_handlers(fields, hist_rows=1)
    handlers[("GET", f"{API}/history/records/attribute")] = (404, {"error": "no route"})
    ops, _, _ = _ops(handlers)
    result = ops.update_card("server", "555", TICKET, fields)
    assert result["verdict"] == VERDICT_RECHECK
    assert result["ticket_records"] is None


def test_update_card_wrong_model_rejected_before_put() -> None:
    fields = {"server_hostname": "n"}
    handlers = _update_handlers(fields, hist_rows=1)
    handlers[("GET", f"{API}/ci/555")] = {"ci": {"_id": 555, "ci_type": 7, **fields}}
    ops, transport, _ = _ops(handlers)
    result = ops.update_card("server", "555", TICKET, fields)
    assert result["verdict"] == VERDICT_REJECT
    assert "PUT" not in _methods(transport)


def test_update_card_rejects_banned_symbol_before_put() -> None:
    ops, transport, _ = _ops(_update_handlers({"server_hostname": "n"}, hist_rows=1))
    result = ops.update_card(
        "server", "555", TICKET, {"server_hostname": f"n{chr(0x1F600)}"}
    )
    assert result["verdict"] == VERDICT_REJECT
    assert "PUT" not in _methods(transport)


# ---------------------------------------------------------------------------
# cmdb_link_relations
# ---------------------------------------------------------------------------


def _link_handlers() -> dict[tuple[str, str], Any]:
    return _handlers(
        {
            ("GET", f"{API}/ci/5"): {"ci": {"_id": 5, "ci_type": 3}},
            ("GET", f"{API}/ci/9"): {"ci": {"_id": 9, "ci_type": 4}},
            ("POST", f"{API}/ci_relations/5/9"): {"id": 321},
        }
    )


def test_link_relations_happy_creates_edge_and_roster_consistency() -> None:
    ops, transport, _ = _ops(_link_handlers())
    result = ops.link_relations(
        TICKET, [{"relation_type_id": 7, "first_ci_id": 5, "second_ci_id": 9}]
    )
    assert result["verdict"] == VERDICT_PASS
    assert result["edges"][0]["edge_id"] == 321
    post = next(r for r in transport.requests if r.method == "POST")
    body = json.loads(post.content)
    assert "relation_type_id" not in body
    assert "ticket_id" not in body
    assert result["ticket_records"] == "not_applicable"
    assert "票号黑洞" in str(result["ticket_records_note"])
    roster = RULES["rules"]["relation_edges"]["edges"]
    listed = any(
        e.get("parent_type") == 3
        and e.get("child_type") == 4
        and e.get("relation_type_id") == 7
        for e in roster
    )
    warn = any("relation_edges" in w for w in result["warnings"])
    assert warn is not listed  # unlisted pair => WARN, listed => silent


def test_link_relations_rejects_bad_ticket_with_zero_requests() -> None:
    ops, transport, _ = _ops(_link_handlers())
    result = ops.link_relations(
        "gov2-b1-s1",
        [{"relation_type_id": 7, "first_ci_id": 5, "second_ci_id": 9}],
    )
    assert result["verdict"] == VERDICT_REJECT
    assert transport.requests == []


def test_link_relations_rejects_dead_endpoint_and_unregistered_type() -> None:
    ops, transport, _ = _ops(_link_handlers())
    result = ops.link_relations(
        TICKET,
        [
            {"relation_type_id": 999, "first_ci_id": 5, "second_ci_id": 9},
            {"relation_type_id": 7, "first_ci_id": 5, "second_ci_id": 40404},
        ],
    )
    assert result["verdict"] == VERDICT_REJECT
    details = [e["detail"] for e in result["edges"]]
    assert any("未在册" in d for d in details)
    assert any("不在活面" in d for d in details)
    assert "POST" not in _methods(transport)


def test_link_relations_partial_success_is_recheck() -> None:
    handlers = _link_handlers()
    handlers[("POST", f"{API}/ci_relations/5/9")] = (500, {"message": "edge boom"})
    handlers[("GET", f"{API}/ci/8")] = {"ci": {"_id": 8, "ci_type": 3}}
    handlers[("POST", f"{API}/ci_relations/5/8")] = {"id": 322}
    ops, _, _ = _ops(handlers)
    result = ops.link_relations(
        TICKET,
        [
            {"relation_type_id": 7, "first_ci_id": 5, "second_ci_id": 9},
            {"relation_type_id": 7, "first_ci_id": 5, "second_ci_id": 8},
        ],
    )
    assert result["verdict"] == VERDICT_RECHECK
    statuses = [e["status"] for e in result["edges"]]
    assert statuses == ["failed", "created"]


# ---------------------------------------------------------------------------
# readonly wiring (intent layer must respect the adapter switch)
# ---------------------------------------------------------------------------


def test_readonly_adapter_blocks_intent_writes_but_not_preflight() -> None:
    from cmdb_mcp_adapter.core.errors import UnsupportedOperationError

    fields = _valid_create_fields()
    ops, transport, _ = _ops(_create_handlers(), readonly=True)
    with pytest.raises(UnsupportedOperationError):
        ops.create_card("server", TICKET, fields)
    with pytest.raises(UnsupportedOperationError):
        ops.update_card("server", "555", TICKET, {"server_hostname": "n"})
    with pytest.raises(UnsupportedOperationError):
        ops.link_relations(
            TICKET, [{"relation_type_id": 7, "first_ci_id": 5, "second_ci_id": 9}]
        )
    assert "POST" not in _methods(transport) and "PUT" not in _methods(transport)
    assert ops.preflight_write("server", fields, TICKET)["verdict"] == VERDICT_PASS


# ---------------------------------------------------------------------------
# create-with-relations leg (edge creation rides the create call)
# ---------------------------------------------------------------------------


def test_create_card_with_relations_creates_edge_after_card_lands() -> None:
    handlers = _create_handlers()
    handlers[("GET", f"{API}/ci/9")] = {"ci": {"_id": 9, "ci_type": 4}}
    handlers[("POST", f"{API}/ci_relations/555/9")] = {"id": 431}
    ops, _, _ = _ops(handlers)
    result = ops.create_card(
        "server",
        TICKET,
        _valid_create_fields(),
        relations=[{"relation_type_id": 7, "second_ci_id": 9, "direction": "out"}],
    )
    assert result["verdict"] == VERDICT_PASS
    assert result["edges"][0]["edge_id"] == 431
    assert result["edges"][0]["first_ci_id"] == 555


def test_create_card_relation_precheck_failure_skips_card_post() -> None:
    handlers = _create_handlers()
    ops, transport, _ = _ops(handlers)
    result = ops.create_card(
        "server",
        TICKET,
        _valid_create_fields(),
        relations=[{"relation_type_id": 888, "second_ci_id": 9, "direction": "out"}],
    )
    assert result["verdict"] == VERDICT_REJECT
    assert result["stage"] == "relations_precheck"
    assert "POST" not in _methods(transport)
