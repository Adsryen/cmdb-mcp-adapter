"""Rule gates fed exclusively by the vendored cmdb rules snapshot.

This module owns every hard rule the intent-level write tools enforce (see
``/tmp/mcpdev/DESIGN-layer3.md``): ticket shape, banned Unicode ranges, note
format, ``is_index`` varchar caps, registered vocabularies, the four-piece
governance column set, and the sensitive-column masking rules.

Discipline (hard boundaries of the task):

* Gate thresholds (32, 160, 128, banned ranges, vocab rosters, four-piece
  columns) are **read from the rules file** — never hard-coded here.  If a
  value cannot be parsed from the file the gate fails closed with
  :class:`ConfigurationError` instead of substituting a guess.
* Values containing banned symbols are **rejected, never stripped**: the gate
  reports the offending code point and its family; it does not rewrite the
  caller's value (silent rewriting would fabricate data).
* Sensitive column values never appear in any message this module produces;
  the only echo form is :func:`mask_value` → ``len=N sha256:<12hex>``.

The rules file itself is a byte-for-byte vendored copy of the ops-repo
generator output (``rules/cmdb_rules.json``); the path may be overridden with
the ``CMDB_RULES_PATH`` environment variable for drift experiments, but the
packaged copy is the production default.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cmdb_mcp_adapter.core.errors import ConfigurationError

#: Environment override for the rules file location (docs/config surface).
ENV_CMDB_RULES_PATH = "CMDB_RULES_PATH"

#: Packaged vendored copy — the production default.
VENDORED_RULES_PATH = Path(__file__).resolve().parent / "rules" / "cmdb_rules.json"

SEVERITY_REJECT = "REJECT"
SEVERITY_WARN = "WARN"


@dataclass(frozen=True)
class GateIssue:
    """One rule-gate finding: severity, stable code, human-readable message."""

    severity: str
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"severity": self.severity, "code": self.code, "message": self.message}


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

_rules_cache: dict[str, dict[str, Any]] = {}


def resolve_rules_path(path: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the rules file path: explicit arg > env > vendored package copy."""
    if path is not None:
        return Path(path)
    env = os.environ.get(ENV_CMDB_RULES_PATH, "").strip()
    if env:
        return Path(env)
    return VENDORED_RULES_PATH


def load_rules(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Load and structurally validate the rules file (cached per resolved path).

    Raises :class:`ConfigurationError` when the file is missing, unparsable,
    or lacks any section the gates read.  There is deliberately no partial
    fallback: a gate that cannot see its numbers must not invent them.
    """
    resolved = resolve_rules_path(path)
    key = str(resolved)
    cached = _rules_cache.get(key)
    if cached is not None:
        return cached
    try:
        raw = resolved.read_bytes()
    except OSError as exc:
        raise ConfigurationError(
            f"cmdb rules file unreadable at {resolved}: {exc}"
        ) from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ConfigurationError(f"cmdb rules file is not valid JSON ({resolved}): {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigurationError(f"cmdb rules file must contain a JSON object ({resolved})")
    _validate_structure(data, resolved)
    _rules_cache[key] = data
    return data


def _validate_structure(data: Mapping[str, Any], resolved: Path) -> None:
    missing: list[str] = []
    for top in ("schema_version", "fingerprints", "rules", "sensitive_columns"):
        if top not in data:
            missing.append(top)
    rules = data.get("rules")
    rule_sections = (
        "ticket",
        "note_format",
        "banned_symbols_T1",
        "field_constraints",
        "vocabulary",
        "four_piece_set",
        "ci_types",
    )
    if isinstance(rules, Mapping):
        missing.extend(f"rules.{name}" for name in rule_sections if name not in rules)
    else:
        missing.append("rules")
    if missing:
        raise ConfigurationError(
            f"cmdb rules file {resolved} is missing required sections: {', '.join(missing)}"
        )


def _rules_section(rules: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    section = rules["rules"][name]
    assert isinstance(section, Mapping)
    return section


# ---------------------------------------------------------------------------
# Parsed gate constants (values come from the file, not this module)
# ---------------------------------------------------------------------------

def ticket_max_len(rules: Mapping[str, Any]) -> int:
    value = _rules_section(rules, "ticket").get("db_max_len")
    if not isinstance(value, int) or value <= 0:
        raise ConfigurationError("rules.ticket.db_max_len is missing or not a positive int")
    return value


def ticket_external_prefix(rules: Mapping[str, Any]) -> str:
    """Prefix of the external ticket form, e.g. ``imp-`` from ``imp-<…>-<…>``."""
    form = _rules_section(rules, "ticket").get("external_form")
    if not isinstance(form, str) or "<" not in form:
        raise ConfigurationError("rules.ticket.external_form is missing or malformed")
    prefix = form.split("<", 1)[0]
    if not prefix:
        raise ConfigurationError("rules.ticket.external_form has no literal prefix before '<'")
    return prefix


def ticket_banned_patterns(rules: Mapping[str, Any]) -> tuple[str, ...]:
    patterns = _rules_section(rules, "ticket").get("banned_for_external")
    if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
        raise ConfigurationError("rules.ticket.banned_for_external is missing or malformed")
    return tuple(patterns)


def _banned_pattern_to_glob(pattern: str) -> str:
    """``gov2-b*`` stays; ``b<序数>`` / ``out/<号>`` placeholders become ``*``."""
    return re.sub(r"<[^>]*>", "*", pattern)


def note_max_len(rules: Mapping[str, Any]) -> int:
    value = _rules_section(rules, "note_format").get("max_len")
    if not isinstance(value, int) or value <= 0:
        raise ConfigurationError("rules.note_format.max_len is missing or not a positive int")
    return value


def note_single_line(rules: Mapping[str, Any]) -> bool:
    return bool(_rules_section(rules, "note_format").get("single_line"))


def _date_tail_pattern(rules: Mapping[str, Any]) -> re.Pattern[str]:
    """Derive the third-segment date check from note_format.three_segment_pattern.

    The literal ``YYYY-MM-DD`` token in the rules text becomes an anchored
    date regex; nothing about the date shape is invented here.
    """
    form = _rules_section(rules, "note_format").get("three_segment_pattern")
    if not isinstance(form, str) or "YYYY-MM-DD" not in form:
        raise ConfigurationError(
            "rules.note_format.three_segment_pattern missing or has no YYYY-MM-DD tail token"
        )
    return re.compile(r"\d{4}-\d{2}-\d{2}\s*$")


def index_text_limit(rules: Mapping[str, Any]) -> int:
    """Parse the varchar(128) cap from the prose gate text (never hard-coded)."""
    section = _rules_section(rules, "field_constraints")
    prose = section.get("index128_gate")
    if not isinstance(prose, str):
        raise ConfigurationError("rules.field_constraints.index128_gate is missing")
    match = re.search(r"varchar\((\d+)\)", prose)
    if match is None:
        raise ConfigurationError(
            "rules.field_constraints.index128_gate carries no varchar(N) cap to parse"
        )
    return int(match.group(1))


def index_text_columns(rules: Mapping[str, Any]) -> frozenset[str]:
    """The roster of ``is_index=1`` text columns from field_constraints."""
    section = _rules_section(rules, "field_constraints")
    roster = section.get("is_index_1_text_attrs")
    if not isinstance(roster, list):
        raise ConfigurationError("rules.field_constraints.is_index_1_text_attrs is missing")
    names = {
        str(entry["attr_name"])
        for entry in roster
        if isinstance(entry, Mapping) and "attr_name" in entry
    }
    if not names:
        raise ConfigurationError("rules.field_constraints.is_index_1_text_attrs is empty")
    return frozenset(names)


def banned_ranges(rules: Mapping[str, Any]) -> tuple[tuple[int, int, str], ...]:
    """``(lo, hi, family)`` inclusive code-point ranges from banned_symbols_T1."""
    section = _rules_section(rules, "banned_symbols_T1")
    ranges_raw = section.get("ranges")
    if not isinstance(ranges_raw, list) or not ranges_raw:
        raise ConfigurationError("rules.banned_symbols_T1.ranges is missing or empty")
    parsed: list[tuple[int, int, str]] = []
    for entry in ranges_raw:
        if not isinstance(entry, Mapping):
            raise ConfigurationError("rules.banned_symbols_T1.ranges has a non-object entry")
        lo, hi, name = entry.get("lo"), entry.get("hi"), entry.get("name")
        if not (isinstance(lo, str) and isinstance(hi, str) and isinstance(name, str)):
            raise ConfigurationError(
                "rules.banned_symbols_T1.ranges entry missing lo/hi/name strings"
            )
        try:
            parsed.append((int(lo[2:], 16), int(hi[2:], 16), name))
        except ValueError as exc:
            raise ConfigurationError(
                f"rules.banned_symbols_T1.ranges hex code point unreadable: {exc}"
            ) from exc
    return tuple(parsed)


# ---------------------------------------------------------------------------
# Attribute / model rosters
# ---------------------------------------------------------------------------

def _attr_flags(rules: Mapping[str, Any]) -> Mapping[str, Mapping[str, Any]]:
    face = _rules_section(rules, "field_constraints").get("attr_flags_live_read_face")
    if not isinstance(face, Mapping) or not isinstance(face.get("attrs"), Mapping):
        raise ConfigurationError("rules.field_constraints.attr_flags_live_read_face.attrs missing")
    attrs: Mapping[str, Mapping[str, Any]] = face["attrs"]
    return attrs


def attr_name_to_id(rules: Mapping[str, Any]) -> dict[str, int]:
    return {
        str(entry["attr_name"]): int(str(attr_id))
        for attr_id, entry in _attr_flags(rules).items()
        if isinstance(entry, Mapping) and "attr_name" in entry
    }


def _vocab_attrs(rules: Mapping[str, Any]) -> Mapping[str, Mapping[str, Any]]:
    vocab = _rules_section(rules, "vocabulary").get("attrs")
    if not isinstance(vocab, Mapping):
        raise ConfigurationError("rules.vocabulary.attrs is missing")
    return vocab


def vocab_entry_for_field(field: str, rules: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Vocabulary entry for a field name, or None when the rules know nothing.

    The dict key in ``rules.vocabulary.attrs`` IS the attribute id, so it is
    injected into the returned entry for stable messages.
    """
    for attr_id, entry in _vocab_attrs(rules).items():
        if isinstance(entry, Mapping) and entry.get("attr_name") == field:
            merged: dict[str, Any] = dict(entry)
            merged.setdefault("attr_id", attr_id)
            return merged
    return None


def dynamic_or_hybrid_attr_ids(rules: Mapping[str, Any]) -> frozenset[int]:
    raw = _rules_section(rules, "vocabulary").get("dynamic_or_hybrid_attrs")
    if not isinstance(raw, list):
        raise ConfigurationError("rules.vocabulary.dynamic_or_hybrid_attrs is missing")
    return frozenset(int(x) for x in raw)


FOUR_PIECE_ROLES = ("lifecycle", "evidence_status", "evidence_source", "checked_at")


def type_id_by_name(model: str, rules: Mapping[str, Any]) -> int | None:
    types = _rules_section(rules, "ci_types").get("types")
    if not isinstance(types, Mapping):
        raise ConfigurationError("rules.ci_types.types is missing")
    for entry in types.values():
        if isinstance(entry, Mapping) and entry.get("name") == model:
            return int(entry["id"])
    return None


def four_piece_bindings(model: str, rules: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    """Governance bindings for a model name; empty tuple when the roster lacks it."""
    type_id = type_id_by_name(model, rules)
    if type_id is None:
        return ()
    per_type = _rules_section(rules, "four_piece_set").get("per_type")
    if not isinstance(per_type, Mapping):
        raise ConfigurationError("rules.four_piece_set.per_type is missing")
    entry = per_type.get(str(type_id))
    if not isinstance(entry, Mapping):
        return ()
    bindings = entry.get("bindings")
    if not isinstance(bindings, list):
        return ()
    return tuple(b for b in bindings if isinstance(b, Mapping))


def unique_key_attr(model: str, rules: Mapping[str, Any]) -> str | None:
    """Unique-key attribute name for dedup lookups (rules.ci_types roster)."""
    types = _rules_section(rules, "ci_types").get("types")
    if not isinstance(types, Mapping):
        raise ConfigurationError("rules.ci_types.types is missing")
    for entry in types.values():
        if isinstance(entry, Mapping) and entry.get("name") == model:
            name = entry.get("unique_attr_name")
            return str(name) if isinstance(name, str) and name else None
    return None


def _notes_attr_names(rules: Mapping[str, Any]) -> frozenset[str]:
    scope = _rules_section(rules, "four_piece_set").get("notes_attr_scope")
    names: set[str] = set()
    if isinstance(scope, Mapping) and isinstance(scope.get("attrs"), list):
        flags = _attr_flags(rules)
        for attr_id in scope["attrs"]:
            entry = flags.get(str(attr_id))
            if isinstance(entry, Mapping) and "attr_name" in entry:
                names.add(str(entry["attr_name"]))
    return frozenset(names)


# ---------------------------------------------------------------------------
# Sensitive columns and masking
# ---------------------------------------------------------------------------

def sensitive_names(rules: Mapping[str, Any]) -> frozenset[str]:
    names: set[str] = set()
    for entry in rules.get("sensitive_columns", []):
        if isinstance(entry, Mapping):
            for key in ("name", "alias"):
                value = entry.get(key)
                if isinstance(value, str) and value:
                    names.add(value)
    return frozenset(names)


def is_sensitive(field: str, rules: Mapping[str, Any]) -> bool:
    return field in sensitive_names(rules)


def mask_value(field: str, value: Any) -> str:
    """The only permitted echo form for a sensitive value: ``len=N sha256:<12>``.

    The field name is accepted for logging symmetry; the raw value is hashed
    (UTF-8) and never appears in the result.
    """
    del field
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"len={len(text)} sha256:{digest[:12]}"


def sha12(value: Any) -> str:
    """First 12 hex chars of the SHA-256 over the stringified cell value."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# The checks (each returns a list of GateIssue; empty list = rule satisfied)
# ---------------------------------------------------------------------------

def check_ticket(ticket: Any, rules: Mapping[str, Any]) -> list[GateIssue]:
    """Required, within the DB column width, external ``imp-*`` form only,
    and not shaped like a governance/internal ticket (banned_for_external)."""
    issues: list[GateIssue] = []
    text = ticket if isinstance(ticket, str) else ""
    if not text.strip():
        return [GateIssue(SEVERITY_REJECT, "TICKET_MISSING", "ticket 必填（票号缺失＝D-17 拒）")]
    limit = ticket_max_len(rules)
    if len(text) > limit:
        issues.append(
            GateIssue(
                SEVERITY_REJECT,
                "TICKET_TOO_LONG",
                f"ticket '{text}' 长 {len(text)} > rules.ticket.db_max_len={limit}"
                "（c_records.ticket_id 列宽）",
            )
        )
    prefix = ticket_external_prefix(rules)
    if not text.startswith(prefix):
        issues.append(
            GateIssue(
                SEVERITY_REJECT,
                "TICKET_NOT_EXTERNAL_FORM",
                f"ticket '{text}' 非外线形制（rules.ticket.external_form 前缀 '{prefix}'）",
            )
        )
    for pattern in ticket_banned_patterns(rules):
        glob = _banned_pattern_to_glob(pattern)
        if fnmatch.fnmatchcase(text, glob):
            issues.append(
                GateIssue(
                    SEVERITY_REJECT,
                    "TICKET_BANNED_FOR_EXTERNAL",
                    f"ticket '{text}' 命中 rules.ticket.banned_for_external "
                    f"模式 '{pattern}'（内部票面禁止走外线）",
                )
            )
            break
    return issues


def check_banned_symbols(value: Any, rules: Mapping[str, Any]) -> list[GateIssue]:
    """Per-code-point check against banned_symbols_T1.ranges.

    REJECT only — the value is never stripped or rewritten here.
    """
    text = value if isinstance(value, str) else str(value) if value is not None else ""
    if not text:
        return []
    ranges = banned_ranges(rules)
    issues: list[GateIssue] = []
    seen: set[str] = set()
    for ch in text:
        code = ord(ch)
        for lo, hi, family in ranges:
            if lo <= code <= hi:
                token = f"U+{code:04X}"
                if token in seen:
                    break
                seen.add(token)
                issues.append(
                    GateIssue(
                        SEVERITY_REJECT,
                        "BANNED_SYMBOL",
                        f"字符 {token}（'{ch}'）命中禁符族 '{family}'"
                        "（rules.banned_symbols_T1）；值只拒不清，未做任何改写",
                    )
                )
                break
    return issues


def _is_note_field(field: str, rules: Mapping[str, Any]) -> bool:
    return field.endswith("_notes") or field in _notes_attr_names(rules)


def check_note_len(field: str, value: Any, rules: Mapping[str, Any]) -> list[GateIssue]:
    """``*_notes`` face: single line, ≤ note_format.max_len; missing third
    (date) segment is a WARN — too many compliant-legacy cells to reject."""
    if not _is_note_field(field, rules):
        return []
    text = value if isinstance(value, str) else str(value) if value is not None else ""
    if not text:
        return []
    issues: list[GateIssue] = []
    limit = note_max_len(rules)
    if len(text) > limit:
        issues.append(
            GateIssue(
                SEVERITY_REJECT,
                "NOTE_TOO_LONG",
                f"{field} 备注长 {len(text)} > rules.note_format.max_len={limit}",
            )
        )
    if note_single_line(rules) and re.search(r"[\r\n]", text):
        issues.append(
            GateIssue(
                SEVERITY_REJECT,
                "NOTE_NOT_SINGLE_LINE",
                f"{field} 备注必须单行（rules.note_format.single_line）",
            )
        )
    if not _date_tail_pattern(rules).search(text):
        issues.append(
            GateIssue(
                SEVERITY_WARN,
                "NOTE_DATE_SEGMENT_MISSING",
                f"{field} 备注缺第三段日期（rules.note_format.three_segment_pattern "
                "'YYYY-MM-DD' 尾段）＝WARN",
            )
        )
    return issues


def check_index_len(field: str, value: Any, rules: Mapping[str, Any]) -> list[GateIssue]:
    """``is_index=1`` text roster (field_constraints.is_index_1_text_attrs):
    cap = varchar(N) parsed from the rules prose."""
    if field not in index_text_columns(rules):
        return []
    text = value if isinstance(value, str) else str(value) if value is not None else ""
    limit = index_text_limit(rules)
    if len(text) > limit:
        return [
            GateIssue(
                SEVERITY_REJECT,
                "INDEX_TEXT_TOO_LONG",
                f"{field} 为 is_index=1 文本列，长 {len(text)} > varchar({limit})"
                "（rules.field_constraints.index128_gate；超长整行会被库拒 1406）",
            )
        ]
    return []


def check_vocab(
    field: str,
    value: Any,
    rules: Mapping[str, Any],
    *,
    live_values: Sequence[str] | None = None,
) -> list[GateIssue]:
    """Vocabulary membership for fields the rules roster knows.

    * static kind → miss is a REJECT (registered values echoed in the message).
    * hybrid kind → static hit passes; otherwise a live lookup must have run:
      miss in the live set is REJECT, no live set is WARN (not verifiable).
    * dynamic kind → live set required; miss is REJECT, absence is WARN.
    """
    entry = vocab_entry_for_field(field, rules)
    if entry is None:
        return []
    text = value if isinstance(value, str) else str(value) if value is not None else ""
    kind = entry.get("kind")
    static_values = entry.get("values")
    static_tuple = tuple(str(v) for v in static_values) if isinstance(static_values, list) else ()
    registered_note = f"在册值清单: {', '.join(static_tuple) if static_tuple else '（静态面为空）'}"
    if kind == "static":
        if text in static_tuple:
            return []
        return [
            GateIssue(
                SEVERITY_REJECT,
                "VOCAB_NOT_REGISTERED",
                f"{field} 值 '{text}' 不在词表（rules.vocabulary kind=static・"
                f"attr_id={entry.get('attr_id')}）；{registered_note}",
            )
        ]
    if kind == "hybrid":
        if text in static_tuple:
            return []
        if live_values is not None:
            live_tuple = tuple(str(v) for v in live_values)
            if text in live_tuple:
                return []
            return [
                GateIssue(
                    SEVERITY_REJECT,
                    "VOCAB_NOT_REGISTERED",
                    f"{field} 值 '{text}' 不在静态册亦不在动态现查源"
                    f"（rules.vocabulary kind=hybrid）；静态{registered_note}",
                )
            ]
        return [
            GateIssue(
                SEVERITY_WARN,
                "VOCAB_DYNAMIC_UNVERIFIED",
                f"{field} 值 '{text}' 不在静态册；hybrid 动态源本次未能现查"
                "＝WARN（不在未验证面上硬拦）",
            )
        ]
    if kind == "dynamic":
        if live_values is not None:
            live_tuple = tuple(str(v) for v in live_values)
            if text in live_tuple:
                return []
            return [
                GateIssue(
                    SEVERITY_REJECT,
                    "VOCAB_NOT_REGISTERED",
                    f"{field} 值 '{text}' 不在动态现查源（rules.vocabulary kind=dynamic）；"
                    f"在册动态值清单: {', '.join(live_tuple) if live_tuple else '（空）'}",
                )
            ]
        return [
            GateIssue(
                SEVERITY_WARN,
                "VOCAB_DYNAMIC_UNVERIFIED",
                f"{field} 为 dynamic 词表列，动态源未能现查＝WARN",
            )
        ]
    return []


def _checked_at_date_pattern(rules: Mapping[str, Any]) -> re.Pattern[str]:
    form = _rules_section(rules, "four_piece_set").get("checked_at_form")
    pattern = form.get("pattern") if isinstance(form, Mapping) else None
    if not isinstance(pattern, str) or "YYYY-MM-DD" not in pattern:
        raise ConfigurationError("rules.four_piece_set.checked_at_form.pattern missing YYYY-MM-DD")
    return re.compile(r"^\d{4}-\d{2}-\d{2}$")


def check_four_piece(
    model: str,
    fields: Mapping[str, Any],
    rules: Mapping[str, Any],
    *,
    for_create: bool,
) -> list[GateIssue]:
    """Four-piece governance columns for a model (rules.four_piece_set).

    On create every role the model roster carries — lifecycle /
    evidence_status / evidence_source / checked_at — must be supplied; a
    missing one is a REJECT named by its column (``evidence_checked_at`` is
    never auto-filled: only the caller knows the first-hand verification
    date).  record_status / notes absence is WARN-only (R-A1 governs the
    record_status face; notes were never a hard presence gate).
    """
    bindings = four_piece_bindings(model, rules)
    if not bindings:
        if for_create and type_id_by_name(model, rules) is not None:
            return [
                GateIssue(
                    SEVERITY_WARN,
                    "FOUR_PIECE_NO_ROSTER",
                    f"模型 '{model}' 在 rules.four_piece_set.per_type 无名单＝四件套存在性闸跳过",
                )
            ]
        return []
    issues: list[GateIssue] = []
    by_role = {str(b.get("role")): b for b in bindings}
    if for_create:
        for role in FOUR_PIECE_ROLES:
            binding = by_role.get(role)
            if binding is None:
                continue
            column = str(binding.get("attr_name"))
            if column not in fields or str(fields.get(column, "")).strip() == "":
                issues.append(
                    GateIssue(
                        SEVERITY_REJECT,
                        "FOUR_PIECE_COLUMN_MISSING",
                        f"create 缺四件套列 '{column}'（role={role}・rules.four_piece_set）"
                        "；evidence_checked_at ⛔ 服务器自取当天钟＝请调用方显式给",
                    )
                )
        for role in ("record_status", "notes"):
            binding = by_role.get(role)
            if binding is None:
                continue
            column = str(binding.get("attr_name"))
            if column not in fields:
                issues.append(
                    GateIssue(
                        SEVERITY_WARN,
                        "GOVERNANCE_COLUMN_MISSING_WARN",
                        f"治理列 '{column}'（role={role}）未提供＝WARN"
                        "（record_status 词面闸 R-A1＝warn_only・notes 存在性非硬闸）",
                    )
                )
    checked_at = by_role.get("checked_at")
    if checked_at is not None and str(checked_at.get("attr_name")) in fields:
        text = str(fields[str(checked_at.get("attr_name"))])
        if text and not _checked_at_date_pattern(rules).match(text):
            issues.append(
                GateIssue(
                    SEVERITY_WARN,
                    "CHECKED_AT_FORMAT",
                    f"{checked_at.get('attr_name')}' 值 '{text}' 非 "
                    "rules.four_piece_set.checked_at_form 的 YYYY-MM-DD 形制",
                )
            )
    return issues


def check_record_status_words(field: str, value: Any, rules: Mapping[str, Any]) -> list[GateIssue]:
    """record_status word face (allowed_words_D66): warn-only per R-A1."""
    markers = _rules_section(rules, "four_piece_set").get("record_status_markers")
    if not isinstance(markers, Mapping):
        return []
    flags = _attr_flags(rules)
    attr_ids = markers.get("attrs")
    names: set[str] = set()
    if isinstance(attr_ids, list):
        for attr_id in attr_ids:
            entry = flags.get(str(attr_id))
            if isinstance(entry, Mapping) and "attr_name" in entry:
                names.add(str(entry["attr_name"]))
    if field not in names:
        return []
    policy = markers.get("write_gate_policy")
    severity = SEVERITY_WARN
    if isinstance(policy, Mapping) and policy.get("reject") is True:
        severity = SEVERITY_REJECT
    allowed = markers.get("allowed_words_D66")
    allowed_set = {str(w) for w in allowed} if isinstance(allowed, list) else set()
    text = value if isinstance(value, str) else str(value) if value is not None else ""
    if text and text not in allowed_set:
        return [
            GateIssue(
                severity,
                "RECORD_STATUS_WORD",
                f"{field} 值 '{text}' 不在 D-66 口径词 {sorted(allowed_set)}"
                "（write_gate_policy=warn_only・R-A1）",
            )
        ]
    return []


def check_value_field(
    field: str,
    value: Any,
    rules: Mapping[str, Any],
    *,
    live_values: Sequence[str] | None = None,
) -> list[GateIssue]:
    """Run every per-value gate for one cell (banned symbols, notes, index,
    vocab, record-status words)."""
    issues: list[GateIssue] = []
    issues.extend(check_banned_symbols(value, rules))
    issues.extend(check_note_len(field, value, rules))
    issues.extend(check_index_len(field, value, rules))
    issues.extend(check_vocab(field, value, rules, live_values=live_values))
    issues.extend(check_record_status_words(field, value, rules))
    return issues


def check_fields(
    model: str,
    fields: Mapping[str, Any],
    ticket: Any,
    rules: Mapping[str, Any],
    *,
    for_create: bool,
    live_values_by_field: Mapping[str, Sequence[str]] | None = None,
    channel: str = "intent",
) -> dict[str, list[GateIssue]]:
    """Whole-payload gate run keyed by field ('' = ticket/payload-level issues).

    Sensitive columns never enter the payload: they are REJECTed as
    write attempts and any echo goes through :func:`mask_value` only.
    """
    per_field: dict[str, list[GateIssue]] = {}
    ticket_issues = check_ticket(ticket, rules)
    if ticket_issues:
        per_field[""] = ticket_issues
    for field, value in fields.items():
        issues: list[GateIssue] = []
        if is_sensitive(field, rules):
            if channel == "raw":
                issues.append(
                    GateIssue(
                        SEVERITY_WARN,
                        "SENSITIVE_WRITE_FORBIDDEN",
                        f"列 '{field}' 为敏感列＝raw 通道放行（票号+改前存档必须齐；"
                        "读侧仍一律 mask）",
                    )
                )
            else:
                issues.append(
                    GateIssue(
                        SEVERITY_REJECT,
                        "SENSITIVE_WRITE_FORBIDDEN",
                        f"列 '{field}' 为敏感列（rules.sensitive_columns）＝⛔ 明文值走本通道"
                        "（写侧拒；读侧一律 mask 为 len=N sha256:<12位>）",
                    )
                )
        else:
            live = (
                live_values_by_field.get(field)
                if live_values_by_field is not None
                else None
            )
            issues.extend(check_value_field(field, value, rules, live_values=live))
        if issues:
            per_field[field] = issues
    four = check_four_piece(model, fields, rules, for_create=for_create)
    for issue in four:
        per_field.setdefault("", []).append(issue)
    return per_field


def has_reject(issues: Iterable[GateIssue]) -> bool:
    return any(issue.severity == SEVERITY_REJECT for issue in issues)


def vocab_source_label(rules: Mapping[str, Any]) -> str:
    stamp = rules.get("generated_at_utc")
    return f"rules@{stamp}" if isinstance(stamp, str) else "rules@unknown"


__all__ = [
    "ENV_CMDB_RULES_PATH",
    "FOUR_PIECE_ROLES",
    "GateIssue",
    "SEVERITY_REJECT",
    "SEVERITY_WARN",
    "VENDORED_RULES_PATH",
    "attr_name_to_id",
    "banned_ranges",
    "check_banned_symbols",
    "check_fields",
    "check_four_piece",
    "check_index_len",
    "check_note_len",
    "check_record_status_words",
    "check_ticket",
    "check_value_field",
    "check_vocab",
    "dynamic_or_hybrid_attr_ids",
    "four_piece_bindings",
    "has_reject",
    "index_text_columns",
    "index_text_limit",
    "is_sensitive",
    "load_rules",
    "mask_value",
    "note_max_len",
    "note_single_line",
    "resolve_rules_path",
    "sensitive_names",
    "sha12",
    "ticket_banned_patterns",
    "ticket_external_prefix",
    "ticket_max_len",
    "type_id_by_name",
    "unique_key_attr",
    "vocab_entry_for_field",
    "vocab_source_label",
]
