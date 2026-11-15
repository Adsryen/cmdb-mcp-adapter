"""Vendored rules-file drift alarm + per-hard-rule PASS/REJECT gate tests.

Design contract (DESIGN-layer3 测试面):
* ``src/cmdb_mcp_adapter/rules/cmdb_rules.json`` is a byte-for-byte copy of
  the ops fact source. ``CMDB_RULES_SOURCE_PATH`` pointing at the ops file
  makes the sha256 collision a HARD failure on drift; unset = skip.
* Every hard rule gets a PASS and a REJECT case, with all thresholds READ
  FROM the rules file — the tests never restate 160/128/32 as literals
  (the only allowed assertion on those numbers is "gate read == vendored
  file read").
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

import pytest

from cmdb_mcp_adapter import rules_gate as rg
from cmdb_mcp_adapter.core.errors import ConfigurationError

SOURCE_ENV = "CMDB_RULES_SOURCE_PATH"


@pytest.fixture(autouse=True)
def _force_default_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whole module runs against the vendored copy, host env be damned."""
    monkeypatch.delenv(rg.ENV_CMDB_RULES_PATH, raising=False)


def _codes(issues: list[rg.GateIssue]) -> set[str]:
    return {i.code for i in issues}


def _rejects(issues: list[rg.GateIssue]) -> list[rg.GateIssue]:
    return [i for i in issues if i.severity == rg.SEVERITY_REJECT]


# ---------------------------------------------------------------------------
# drift alarm: presence, structure, sha collision with the ops source
# ---------------------------------------------------------------------------


def test_vendored_copy_present_and_parseable() -> None:
    assert rg.VENDORED_RULES_PATH.is_file(), (
        "vendored rules copy missing: " + str(rg.VENDORED_RULES_PATH)
    )
    data = json.loads(rg.VENDORED_RULES_PATH.read_text(encoding="utf-8"))
    for key in (
        "schema_version",
        "fingerprints",
        "consistency_flags",
        "rules",
        "sensitive_columns",
    ):
        assert key in data, f"vendored rules lost top-level key '{key}'"
    assert re.fullmatch(r"[0-9a-f]{64}", str(data["fingerprints"]["spec_sha256"]))


def test_vendored_vs_ops_source_sha_collision() -> None:
    """RED when the ops source drifted and the copy was not re-pulled."""
    source = os.environ.get(SOURCE_ENV)
    if not source:
        pytest.skip(f"{SOURCE_ENV} unset — ops source not on this machine")
    source_path = Path(source)
    if not source_path.is_file():
        pytest.skip(f"{SOURCE_ENV} points at a missing file")
    vendored_sha = hashlib.sha256(rg.VENDORED_RULES_PATH.read_bytes()).hexdigest()
    source_sha = hashlib.sha256(source_path.read_bytes()).hexdigest()
    assert vendored_sha == source_sha, (
        "vendored rules copy is DRIFTED from the ops fact source "
        f"({source_path}); re-copy byte-for-byte, ⛔ hand-edit"
    )


def test_gate_thresholds_equal_vendored_file_values() -> None:
    """The one allowed numeric assertion: gate reads == raw file reads."""
    raw = json.loads(rg.VENDORED_RULES_PATH.read_text(encoding="utf-8"))
    rules = rg.load_rules()
    assert rg.ticket_max_len(rules) == raw["rules"]["ticket"]["db_max_len"]
    assert rg.note_max_len(rules) == raw["rules"]["note_format"]["max_len"]
    prose = raw["rules"]["field_constraints"]["index128_gate"]
    varchar = re.search(r"varchar\((\d+)\)", str(prose))
    assert varchar is not None, "rules prose lost varchar(N) — gate fails closed"
    assert rg.index_text_limit(rules) == int(varchar.group(1))


def test_env_override_and_missing_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(rg.ENV_CMDB_RULES_PATH, str(rg.VENDORED_RULES_PATH))
    assert rg.resolve_rules_path() == rg.VENDORED_RULES_PATH
    monkeypatch.setenv(rg.ENV_CMDB_RULES_PATH, "/nonexistent/rules-drift.json")
    with pytest.raises(ConfigurationError):
        rg.load_rules()


# ---------------------------------------------------------------------------
# hard rule 1: ticket form (rules.ticket)
# ---------------------------------------------------------------------------


def test_ticket_pass_external_imp_form() -> None:
    rules = rg.load_rules()
    prefix = rg.ticket_external_prefix(rules)
    limit = rg.ticket_max_len(rules)
    ticket = (prefix + "testrunner-20261115-120000")[:limit]
    assert rg.check_ticket(ticket, rules) == []


def test_ticket_rejects_missing_too_long_internal_forms() -> None:
    rules = rg.load_rules()
    prefix = rg.ticket_external_prefix(rules)
    limit = rg.ticket_max_len(rules)
    assert _codes(rg.check_ticket("", rules)) == {"TICKET_MISSING"}
    assert _codes(rg.check_ticket(None, rules)) == {"TICKET_MISSING"}
    long_ticket = prefix + "y" * (limit + 1 - len(prefix))
    assert "TICKET_TOO_LONG" in _codes(rg.check_ticket(long_ticket, rules))
    gov = "gov2-b1-s1"
    gov_codes = _codes(rg.check_ticket(gov, rules))
    assert "TICKET_NOT_EXTERNAL_FORM" in gov_codes
    assert "TICKET_BANNED_FOR_EXTERNAL" in gov_codes


def test_ticket_banned_patterns_all_reject() -> None:
    """Every rules.ticket.banned_for_external pattern must gate its own shape."""
    rules = rg.load_rules()
    for pattern in rg.ticket_banned_patterns(rules):
        candidate = pattern.replace("*", "1x")
        candidate = re.sub(r"<[^>]*>", "1", candidate)
        codes = _codes(rg.check_ticket(candidate, rules))
        assert "TICKET_BANNED_FOR_EXTERNAL" in codes, (
            f"pattern '{pattern}' sample '{candidate}' not rejected"
        )


# ---------------------------------------------------------------------------
# hard rule 2: banned symbols (rules.banned_symbols_T1) — REJECT only
# ---------------------------------------------------------------------------


def test_banned_symbols_reject_first_char_of_every_range() -> None:
    rules = rg.load_rules()
    ranges = rg.banned_ranges(rules)
    assert ranges, "rules lost banned ranges — gate would silently pass"
    for lo, _hi, family in ranges:
        ch = chr(lo)
        issues = rg.check_banned_symbols(f"前段{ch}后段", rules)
        rejects = _rejects(issues)
        assert len(rejects) == 1, f"U+{lo:04X} ({family}) not rejected"
        assert rejects[0].code == "BANNED_SYMBOL"
        assert family in rejects[0].message
        assert f"U+{lo:04X}" in rejects[0].message


def test_banned_symbols_pass_on_plain_and_exception_chars() -> None:
    rules = rg.load_rules()
    assert rg.check_banned_symbols("普通文本 abc-1 *（）；", rules) == []
    raw = json.loads(rg.VENDORED_RULES_PATH.read_text(encoding="utf-8"))
    for token in raw["rules"]["banned_symbols_T1"]["explicitly_not_banned"]["chars"]:
        ch = chr(int(str(token)[2:], 16))
        assert rg.check_banned_symbols(f"a{ch}b", rules) == [], (
            f"explicitly_not_banned '{token}' wrongly rejected"
        )


# ---------------------------------------------------------------------------
# hard rule 3: notes face (rules.note_format)
# ---------------------------------------------------------------------------

NOTE_FIELD = "server_notes"


def test_note_pass_with_rules_verbatim_example() -> None:
    rules = rg.load_rules()
    raw = json.loads(rg.VENDORED_RULES_PATH.read_text(encoding="utf-8"))
    example = str(raw["rules"]["note_format"]["example_verbatim"])
    issues = rg.check_note_len(NOTE_FIELD, example, rules)
    assert issues == []


def test_note_rejects_overlong_and_multiline_warns_missing_date() -> None:
    rules = rg.load_rules()
    limit = rg.note_max_len(rules)
    over = "事；出处＝实测；2026-01-01" + "x" * (limit + 1)
    assert "NOTE_TOO_LONG" in _codes(rg.check_note_len(NOTE_FIELD, over, rules))
    multiline = "第一行\n第二行；2026-01-01"
    assert "NOTE_NOT_SINGLE_LINE" in _codes(
        rg.check_note_len(NOTE_FIELD, multiline, rules)
    )
    no_date = rg.check_note_len(NOTE_FIELD, "事实；出处＝实测", rules)
    assert _rejects(no_date) == []
    assert "NOTE_DATE_SEGMENT_MISSING" in _codes(no_date)


# ---------------------------------------------------------------------------
# hard rule 4: is_index=1 text cap (rules.field_constraints)
# ---------------------------------------------------------------------------


def test_index_text_roster_cap_boundary() -> None:
    rules = rg.load_rules()
    limit = rg.index_text_limit(rules)
    columns = sorted(rg.index_text_columns(rules))
    assert columns, "rules lost is_index_1_text_attrs roster"
    column = columns[0]
    assert rg.check_index_len(column, "x" * limit, rules) == []
    assert "INDEX_TEXT_TOO_LONG" in _codes(rg.check_index_len(column, "x" * (limit + 1), rules))


def test_index_cap_only_applies_to_roster_columns() -> None:
    rules = rg.load_rules()
    limit = rg.index_text_limit(rules)
    outsider = "not_an_indexed_attr"
    assert outsider not in rg.index_text_columns(rules)
    assert rg.check_index_len(outsider, "x" * (limit * 3), rules) == []


# ---------------------------------------------------------------------------
# hard rule 5: vocabulary (rules.vocabulary) — static reject / live hybrid
# ---------------------------------------------------------------------------


def _vocab_entries(rules: dict[str, Any]) -> dict[str, dict[str, Any]]:
    section = rules["rules"]["vocabulary"]["attrs"]
    assert isinstance(section, dict)
    return {str(k): dict(v) for k, v in section.items()}


def test_vocab_static_hit_passes_miss_rejects_with_registered_list() -> None:
    rules = rg.load_rules()
    entries = _vocab_entries(rules)
    field, entry = next(
        (e["attr_name"], e)
        for e in entries.values()
        if e.get("kind") == "static" and e.get("values")
    )
    values = [str(v) for v in entry["values"]]
    assert rg.check_vocab(field, values[0], rules) == []
    miss = f"⊘-not-registered-{values[0]}"
    issues = rg.check_vocab(field, miss, rules)
    rejects = _rejects(issues)
    assert rejects and rejects[0].code == "VOCAB_NOT_REGISTERED"
    assert values[0] in rejects[0].message, "REJECT must echo the registered values"


def test_vocab_hybrid_prefers_static_then_live_then_warns() -> None:
    rules = rg.load_rules()
    entries = _vocab_entries(rules)
    field, entry = next(
        (e["attr_name"], e)
        for e in entries.values()
        if e.get("kind") == "hybrid" and e.get("values")
    )
    static_value = str(entry["values"][0])
    assert rg.check_vocab(field, static_value, rules) == []
    outsider = "只在动态源在册的项目代号"
    assert rg.check_vocab(field, outsider, rules, live_values=[outsider]) == []
    misses = _rejects(rg.check_vocab(field, outsider, rules, live_values=["别的"]))
    assert misses and misses[0].code == "VOCAB_NOT_REGISTERED"
    warns = rg.check_vocab(field, outsider, rules)
    assert _rejects(warns) == []
    assert "VOCAB_DYNAMIC_UNVERIFIED" in _codes(warns)


def test_vocab_dynamic_requires_live_source_else_warn() -> None:
    rules = rg.load_rules()
    entries = _vocab_entries(rules)
    field = next(
        e["attr_name"] for e in entries.values() if e.get("kind") == "dynamic"
    )
    warns = rg.check_vocab(field, "任意项目", rules)
    assert _rejects(warns) == []
    assert "VOCAB_DYNAMIC_UNVERIFIED" in _codes(warns)
    ok = rg.check_vocab(field, "任意项目", rules, live_values=["任意项目"])
    assert ok == []
    bad = _rejects(rg.check_vocab(field, "没登记", rules, live_values=["任意项目"]))
    assert bad and bad[0].code == "VOCAB_NOT_REGISTERED"


# ---------------------------------------------------------------------------
# hard rule 6: four-piece face (rules.four_piece_set)
# ---------------------------------------------------------------------------

FOUR_PIECE_MODEL = "server"


def _four_piece_fields(rules: dict[str, Any], *, drop_role: str | None = None) -> dict[str, str]:
    fields: dict[str, str] = {}
    for binding in rg.four_piece_bindings(FOUR_PIECE_MODEL, rules):
        role = str(binding.get("role"))
        if role == drop_role:
            continue
        name = str(binding.get("attr_name"))
        fields[name] = "2026-01-02" if role == "checked_at" else "x"
    return fields


def test_four_piece_create_needs_every_hard_role_column() -> None:
    rules = rg.load_rules()
    hard_roles = set(rg.FOUR_PIECE_ROLES)
    roster = {str(b.get("role")) for b in rg.four_piece_bindings(FOUR_PIECE_MODEL, rules)}
    expected = hard_roles & roster
    assert expected, "model server carries no hard four-piece roles?"
    complete = rg.check_four_piece(
        FOUR_PIECE_MODEL, _four_piece_fields(rules), rules, for_create=True
    )
    assert _rejects(complete) == []
    for role in sorted(expected):
        missing = _four_piece_fields(rules, drop_role=role)
        rejects = _rejects(
            rg.check_four_piece(FOUR_PIECE_MODEL, missing, rules, for_create=True)
        )
        column = next(
            str(b["attr_name"])
            for b in rg.four_piece_bindings(FOUR_PIECE_MODEL, rules)
            if b.get("role") == role
        )
        assert any(
            r.code == "FOUR_PIECE_COLUMN_MISSING" and column in r.message
            for r in rejects
        ), f"missing role {role} must REJECT naming '{column}'"


def test_four_piece_checked_at_never_server_auto_filled_format_warn() -> None:
    rules = rg.load_rules()
    fields = _four_piece_fields(rules)
    checked_at = next(
        str(b["attr_name"])
        for b in rg.four_piece_bindings(FOUR_PIECE_MODEL, rules)
        if b.get("role") == "checked_at"
    )
    fields[checked_at] = "01/2026"
    issues = rg.check_four_piece(FOUR_PIECE_MODEL, fields, rules, for_create=True)
    assert _rejects(issues) == [], "date form is a WARN face, not a REJECT"
    assert "CHECKED_AT_FORMAT" in _codes(issues)


def test_four_piece_update_not_forced_complete() -> None:
    rules = rg.load_rules()
    issues = rg.check_four_piece(FOUR_PIECE_MODEL, {}, rules, for_create=False)
    assert "FOUR_PIECE_COLUMN_MISSING" not in _codes(issues)


# ---------------------------------------------------------------------------
# hard rule 7: record_status words stay warn-only (R-A1) — severity follows file
# ---------------------------------------------------------------------------


def test_record_status_word_severity_follows_write_gate_policy() -> None:
    rules = rg.load_rules()
    raw = json.loads(rg.VENDORED_RULES_PATH.read_text(encoding="utf-8"))
    markers = raw["rules"]["four_piece_set"]["record_status_markers"]
    flags = rules["rules"]["field_constraints"]["attr_flags_live_read_face"]["attrs"]
    field = str(flags[str(markers["attrs"][0])]["attr_name"])
    allowed = {str(w) for w in markers["allowed_words_D66"]}
    assert rg.check_record_status_words(field, sorted(allowed)[0], rules) == []
    issues = rg.check_record_status_words(field, "口径外新词", rules)
    assert issues and issues[0].code == "RECORD_STATUS_WORD"
    expected = rg.SEVERITY_REJECT if markers["write_gate_policy"]["reject"] else rg.SEVERITY_WARN
    assert issues[0].severity == expected


# ---------------------------------------------------------------------------
# hard rule 8: sensitive columns — reject on write, mask on echo
# ---------------------------------------------------------------------------


def test_sensitive_write_rejected_never_echoed() -> None:
    rules = rg.load_rules()
    raw = json.loads(rg.VENDORED_RULES_PATH.read_text(encoding="utf-8"))
    entry = raw["sensitive_columns"][0]
    name = str(entry["name"])
    alias = str(entry.get("alias", ""))
    assert rg.is_sensitive(name, rules)
    assert alias and rg.is_sensitive(alias, rules)
    assert not rg.is_sensitive("server_resource_id", rules)
    secret = "TOP-SECRET-PLAINTEXT-VALUE"
    per_field = rg.check_fields(
        "credential", {name: secret}, "imp-testrunner-20261115-120000", rules,
        for_create=False,
    )
    rejects = _rejects(per_field.get(name, []))
    assert rejects and rejects[0].code == "SENSITIVE_WRITE_FORBIDDEN"
    assert all(secret not in i.message for i in rejects), "value echoed in REJECT msg"


def test_mask_value_shape_len_plus_sha12() -> None:
    rules = rg.load_rules()
    name = rg.sensitive_names(rules)
    field = sorted(name)[0]
    masked = rg.mask_value(field, "anything-at-all")
    assert re.fullmatch(r"len=\d+ sha256:[0-9a-f]{12}", masked)
    assert masked == rg.mask_value(field, "anything-at-all")
    assert masked != rg.mask_value(field, "anything-else")
