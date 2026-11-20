"""Intent-level write tools for the VeOps adapter (layer-3 gates).

These operations sink the rules-gate harness that used to live in the
ops-repo executor scripts into the adapter itself, so *every* MCP client
gets the same gate set.  The four public entry points mirror the MCP tools
``cmdb_preflight_write`` / ``cmdb_create_card`` / ``cmdb_update_card`` /
``cmdb_link_relations``.

Contract with the design (``/tmp/mcpdev/DESIGN-layer3.md``):

* ``cmdb_preflight_write`` performs **zero write HTTP** — only current-state
  reads (GET) — and returns the full verdict (``PASS`` / ``REJECT``) with
  per-cell ``errs`` / ``warns``.
* ``cmdb_create_card`` runs every gate, then ``POST /ci``, then reads the
  card back and hashes every touched cell (``cells_readback``), reports the
  ticket's record rows (``ticket_records``) and creates optional edges.
  On an upstream 5xx/timeout it first **reverse-looks-up the unique key**
  before raising — and never advises a blind replay, because ``POST /ci``
  is not transactionally atomic upstream (a 500 may still have persisted).
* ``cmdb_update_card`` adds the third ruler: the attribute-history row count
  must equal the number of cells actually sent; a mismatch yields
  ``RECHECK`` instead of ``PASS`` (this is the "200 empty write" guard).
* ``cmdb_link_relations`` verifies both endpoints are alive and the relation
  type is registered, then creates the edge with an empty body ``{}`` (lead
  live-verified 2026-11-15: the platform infers the relation type from the
  endpoint type pair; body keys are wrong).  Edge writes leave no c_records
  rows ("ticket black hole"), so ``ticket_records`` is reported as
  ``not_applicable`` here — never a misleading 0.  It never deletes anything.

All numeric gate thresholds come from the vendored rules file through
:mod:`cmdb_mcp_adapter.rules_gate` — nothing here hard-codes 160/128/32 or
column rosters.  Sensitive column values are never echoed: the write side
refuses them outright, and every read echo uses ``len``/``sha`` forms only.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from cmdb_mcp_adapter import rules_gate as rg
from cmdb_mcp_adapter.adapters.veops import mapper
from cmdb_mcp_adapter.core.errors import (
    CoreError,
    NotFoundError,
    TimeoutError,
    UpstreamServerError,
    ValidationError,
)

if TYPE_CHECKING:  # pragma: no cover
    from cmdb_mcp_adapter.adapters.veops.adapter import VeopsAdapter
    from cmdb_mcp_adapter.adapters.veops.client import VeopsClient

#: History endpoint carrying c_records meta rows (ticket_id) per CI.  Route
#: and ``[meta,[hist…]]`` shape from the 2.6.2 UI bundle; ``meta.ticket_id``
#: is CODE-VERIFIED (lead 2026-11-15: veops-cmdb-api
#: /data/apps/cmdb/api/lib/cmdb/history.py:95/:152 — ``record_dict =
#: record.OperationRecord.to_dict()``, the row carries the ticket_id column).
#: Fake-transport tests pin the shape used here.
_PATH_HISTORY_ATTRIBUTE = "/history/records/attribute"

#: Single-edge creation URL form (both numbers only, per §13.8 L383).  Body
#: is ``{}`` — platform infers the relation type from the endpoint type pair
#: (lead live-verified 2026-11-15, 11 edges).
_PATH_EDGE_CREATE = "/ci_relations/{first_ci_id}/{second_ci_id}"

VERDICT_PASS = "PASS"
VERDICT_REJECT = "REJECT"
VERDICT_RECHECK = "RECHECK"


def _as_digits(value: Any, field: str) -> str:
    """Accept int or digit-string identifiers and return the digit string."""
    if isinstance(value, bool):
        raise ValidationError(f"{field} must be an integer or digit string")
    if isinstance(value, int):
        if value <= 0:
            raise ValidationError(f"{field} must be a positive integer")
        return str(value)
    if isinstance(value, str) and value.isdigit():
        return value
    raise ValidationError(f"{field} must be an integer or digit string, got {type(value).__name__}")


def _cell_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


class VeopsIntentOps:
    """Rules-gated intent operations over a :class:`VeopsAdapter`."""

    def __init__(self, client: VeopsClient, adapter: VeopsAdapter) -> None:
        self._client = client
        self._adapter = adapter
        self._rules = rg.load_rules()
        self._alive_cache: dict[str, dict[str, Any] | None] = {}

    # ------------------------------------------------------------------
    # public entries (argument shape is already contract-validated by
    # CoreService; these methods raise CoreError subclasses on bad shape)
    # ------------------------------------------------------------------

    def raw_write_gate(
        self,
        model_name: str,
        data: dict[str, Any],
        *,
        for_create: bool,
    ) -> None:
        """0.4.0: raw create/update share the field gate with the intent channel.

        Sensitive columns degrade to WARN only on this channel (the raw leg is
        the sanctioned credential write path, backed by ticket + pre-change
        archive); every other REJECT stands.  Raises ``ValidationError`` listing
        stable codes + field names — never payload values.
        """
        fields = {k: v for k, v in data.items() if k != "ticket_id"}
        ticket = data.get("ticket_id")
        issues_by_field = rg.check_fields(
            model_name, fields, ticket, self._rules,
            for_create=for_create,
            live_values_by_field=self._live_values_for(fields),
            channel="raw",
        )
        rejects = sorted(
            {f"{issue.code}({name or 'payload'})"
             for name, issues in issues_by_field.items()
             for issue in issues
             if issue.severity == rg.SEVERITY_REJECT}
        )
        if rejects:
            raise ValidationError("raw write gate REJECT: " + ", ".join(rejects))

    def preflight_write(
        self,
        model_name: str,
        fields: dict[str, Any],
        ticket: str,
        ci_id: str | None = None,
    ) -> dict[str, Any]:
        """Zero-write verdict: current-state reads + every gate, pre-flight."""
        self._adapter._resolve_type_id(model_name)
        for_create = ci_id is None
        raw_before: dict[str, Any] | None = None
        existence_issues: list[rg.GateIssue] = []
        if ci_id is not None:
            raw_before = self._read_instance_raw_or_none(ci_id)
            if raw_before is None:
                existence_issues.append(
                    rg.GateIssue(
                        rg.SEVERITY_REJECT,
                        "CI_NOT_FOUND",
                        f"ci_id={ci_id} 不存在（GET /ci/<id> 404）",
                    )
                )
        roster_issues_by_field, field_names_known = self._field_roster_issues(
            model_name, fields
        )
        dedup_issues: list[rg.GateIssue] = []
        dedup_field: str | None = None
        if for_create:
            dedup_issues, dedup_field = self._dedup_issues(model_name, fields)
        issues_by_field = rg.check_fields(
            model_name,
            fields,
            ticket,
            self._rules,
            for_create=for_create,
            live_values_by_field=self._live_values_for(fields),
        )
        for field_key, issues in roster_issues_by_field.items():
            issues_by_field.setdefault(field_key, []).extend(issues)
        for issue in existence_issues:
            issues_by_field.setdefault("", []).append(issue)
        if dedup_issues and dedup_field is not None:
            issues_by_field.setdefault(dedup_field, []).extend(dedup_issues)
        cells = self._build_cells(fields, raw_before, issues_by_field)
        verdict = VERDICT_REJECT if rg.has_reject(
            issue for issues in issues_by_field.values() for issue in issues
        ) else VERDICT_PASS
        return {
            "verdict": verdict,
            "mode": "create" if for_create else "update",
            "model_name": model_name,
            "ci_id": ci_id,
            "cells": cells,
            "vocab_source": rg.vocab_source_label(self._rules),
            "known_fields_unverified": not field_names_known,
        }

    def create_card(
        self,
        model_name: str,
        ticket: str,
        fields: dict[str, Any],
        relations: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Gated create: gates → POST /ci → per-cell SHA readback → edges."""
        self._adapter._check_writable("cmdb_create_card")
        guard = self._ticket_field_guard(fields)
        if guard is not None:
            return guard
        type_id = self._adapter._resolve_type_id(model_name)
        verdict = self.preflight_write(model_name, fields, ticket, ci_id=None)
        if verdict["verdict"] == VERDICT_REJECT:
            return {
                "verdict": VERDICT_REJECT,
                "stage": "gate",
                "cells": verdict["cells"],
                "vocab_source": verdict["vocab_source"],
                "note": "闸未过＝零写入（未发 POST）",
            }
        edge_pre = self._precheck_create_relations(relations)
        if edge_pre["verdict"] == VERDICT_REJECT:
            return {
                "verdict": VERDICT_REJECT,
                "stage": "relations_precheck",
                "relations": edge_pre["checked"],
                "note": "关系腿预检失败＝零写入（未发 POST /ci）",
            }
        body: dict[str, Any] = {"ci_type": type_id, **fields, "ticket_id": ticket}
        try:
            payload = self._client.request("POST", "/ci", json_body=body)
        except (UpstreamServerError, TimeoutError) as exc:
            # Ambiguous face: the row may still have landed.  Reverse-look up
            # the unique key before surfacing the error; never blind-replay.
            raise self._create_failure_with_reverse_lookup(model_name, fields, exc) from exc
        ci_id_raw = payload.get("ci_id") if isinstance(payload, dict) else None
        if ci_id_raw is None and isinstance(payload, dict):
            ci_id_raw = payload.get("_id") or payload.get("id")
        try:
            ci_id = _as_digits(ci_id_raw, "ci_id (create response)")
        except ValidationError as exc:
            raise self._create_failure_with_reverse_lookup(model_name, fields, exc) from exc
        warnings: list[str] = list(edge_pre["warnings"])
        readback = self._readback_cells(ci_id, fields)
        counts = self._ticket_counts(ci_id, ticket)
        attr_hist = counts["attr_hist_rows"]
        reasons: list[str] = []
        verdict_out = VERDICT_PASS
        if readback is None:
            verdict_out = VERDICT_RECHECK
            reasons.append("逐格 SHA 回读未完成（GET /ci/<id> 失败）")
        elif readback["mismatch"]:
            verdict_out = VERDICT_RECHECK
            reasons.append(
                f"逐格 SHA 回读 {readback['ok']}/{readback['total']} "
                f"不一致格: {readback['mismatch']}"
            )
        if counts["ticket_records"] is None:
            verdict_out = VERDICT_RECHECK
            reasons.append(
                f"ticket_records 不可证（{counts.get('history_error') or '历史计数接口失败'}）"
            )
        elif counts["ticket_records"] < 1:
            verdict_out = VERDICT_RECHECK
            reasons.append("ticket_records=0（票面未落 c_records）")
        if attr_hist is None:
            warnings.append("attr_hist_rows 不可证（形态待现网核实）")
        edges = self._create_edges_for_card(ci_id, relations, ticket)
        for edge in edges:
            if edge["status"] != "created":
                warnings.append(
                    f"建边失败 relation_type_id={edge['relation_type_id']} "
                    f"second={edge['other_ci_id']}: {edge['detail']}"
                )
        result: dict[str, Any] = {
            "verdict": verdict_out,
            "ci_id": ci_id,
            "cells_readback": (
                f"{readback['ok']}/{readback['total']}" if readback else "unverifiable"
            ),
            "ticket_records": counts["ticket_records"],
            "attr_hist_rows": attr_hist,
            "edges": edges,
            "warnings": warnings,
            "vocab_source": rg.vocab_source_label(self._rules),
        }
        if reasons:
            result["recheck_reasons"] = reasons
        return result

    def update_card(
        self,
        model_name: str,
        ci_id: str,
        ticket: str,
        fields: dict[str, Any],
    ) -> dict[str, Any]:
        """Gated update: gates → PUT → three rulers (cell SHA, ticket rows,
        attribute-history rows == changed cells)."""
        self._adapter._check_writable("cmdb_update_card")
        guard = self._ticket_field_guard(fields)
        if guard is not None:
            return guard
        type_id = self._adapter._resolve_type_id(model_name)
        verdict = self.preflight_write(model_name, fields, ticket, ci_id=ci_id)
        if verdict["verdict"] == VERDICT_REJECT:
            return {
                "verdict": VERDICT_REJECT,
                "stage": "gate",
                "cells": verdict["cells"],
                "vocab_source": verdict["vocab_source"],
                "note": "闸未过＝零写入（未发 PUT）",
            }
        raw_before = self._read_instance_raw_or_none(ci_id)
        if raw_before is None:
            return {"verdict": VERDICT_REJECT, "stage": "gate", "note": f"ci_id={ci_id} 不存在"}
        live_type = raw_before.get("ci_type")
        live_type_mismatch = (
            isinstance(live_type, (int, str))
            and str(live_type).isdigit()
            and int(live_type) != type_id
        )
        if live_type_mismatch:
            return {
                "verdict": VERDICT_REJECT,
                "stage": "gate",
                "note": (
                    f"ci_id={ci_id} 的 ci_type={live_type} 与 "
                    f"model_name='{model_name}'(id={type_id}) 不符"
                ),
            }
        body: dict[str, Any] = {**fields, "ticket_id": ticket}
        self._client.request("PUT", f"/ci/{ci_id}", json_body=body)
        warnings: list[str] = []
        reasons: list[str] = []
        readback = self._readback_cells(ci_id, fields)
        counts = self._ticket_counts(ci_id, ticket)
        verdict_out = VERDICT_PASS
        if readback is None:
            verdict_out = VERDICT_RECHECK
            reasons.append("逐格 SHA 回读未完成")
        elif readback["mismatch"]:
            verdict_out = VERDICT_RECHECK
            reasons.append(
                f"逐格 SHA 回读 {readback['ok']}/{readback['total']} "
                f"不一致格: {readback['mismatch']}"
            )
        if counts["ticket_records"] is None:
            verdict_out = VERDICT_RECHECK
            reasons.append(
                f"ticket_records 不可证（{counts.get('history_error') or '历史计数接口失败'}）"
            )
        elif counts["ticket_records"] < 1:
            verdict_out = VERDICT_RECHECK
            reasons.append("ticket_records=0")
        changed = len(fields)
        if counts["attr_hist_rows"] is None:
            verdict_out = VERDICT_RECHECK
            reasons.append(
                f"attr_hist_rows 不可证（无法证明非空改＝§17.3 200空改防护失效；"
                f"{counts.get('history_error') or '历史计数接口失败'}）"
            )
        elif counts["attr_hist_rows"] != changed:
            verdict_out = VERDICT_RECHECK
            reasons.append(
                f"属性史行数 {counts['attr_hist_rows']} ≠ 改动格数 {changed}"
                "（静默空改嫌疑＝RECHECK 非 PASS）"
            )
        result: dict[str, Any] = {
            "verdict": verdict_out,
            "ci_id": ci_id,
            "cells_readback": (
                f"{readback['ok']}/{readback['total']}" if readback else "unverifiable"
            ),
            "ticket_records": counts["ticket_records"],
            "attr_hist_rows": counts["attr_hist_rows"],
            "warnings": warnings,
            "vocab_source": rg.vocab_source_label(self._rules),
        }
        if reasons:
            result["recheck_reasons"] = reasons
        return result

    def link_relations(self, ticket: str, edges: list[dict[str, Any]]) -> dict[str, Any]:
        """Verify endpoints alive + type registered, then create each edge.

        Edge body is ``{}`` — lead 2026-11-15 live verified (11 edges,
        ci_relations 7991–8001): the platform infers the relation type from
        the endpoint type pair; passing relation_type_id/ticket_id in the
        body is wrong.  Edge writes leave ZERO rows in c_records (same-day
        measured: 20 edges → 0 ticket record rows; cards write fine) — the
        platform-side edge write never carried a ticket, so this tool reports
        ``ticket_records = "not_applicable"`` plus a fixed explanation instead
        of a misleading 0.  The ticket parameter stays gated (trace head,
        banned forms) because edge audit trails route through it in the
        planned write-gate (p22 草案处理中).
        """
        self._adapter._check_writable("cmdb_link_relations")
        ticket_issues = rg.check_ticket(ticket, self._rules)
        if rg.has_reject(ticket_issues):
            return {
                "verdict": VERDICT_REJECT,
                "stage": "gate",
                "issues": [i.to_dict() for i in ticket_issues],
                "note": "票号闸未过＝零写入",
            }
        type_map = self._relation_type_map()
        results: list[dict[str, Any]] = []
        warnings: list[str] = []
        created_count = 0
        attempted_count = 0
        for raw_edge in edges:
            rtid = _as_digits(raw_edge.get("relation_type_id"), "relation_type_id")
            first = _as_digits(raw_edge.get("first_ci_id"), "first_ci_id")
            second = _as_digits(raw_edge.get("second_ci_id"), "second_ci_id")
            entry: dict[str, Any] = {
                "relation_type_id": int(rtid),
                "first_ci_id": int(first),
                "second_ci_id": int(second),
                "status": "created",
                "edge_id": None,
                "detail": "",
            }
            problems: list[str] = []
            if rtid not in {str(k) for k in type_map}:
                problems.append(f"relation_type_id={rtid} 未在册（GET /relation_types 现读）")
            f_raw = self._read_instance_raw_or_none(first)
            if f_raw is None:
                problems.append(f"first_ci_id={first} 不在活面（GET /ci/<id> 404）")
            s_raw = self._read_instance_raw_or_none(second)
            if s_raw is None:
                problems.append(f"second_ci_id={second} 不在活面（GET /ci/<id> 404）")
            roster_warning = self._edge_roster_warning(f_raw, s_raw, int(rtid))
            if roster_warning:
                warnings.append(roster_warning)
            if problems:
                entry["status"] = "rejected"
                entry["detail"] = "；".join(problems)
                results.append(entry)
                continue
            attempted_count += 1
            try:
                path = _PATH_EDGE_CREATE.format(first_ci_id=first, second_ci_id=second)
                payload = self._client.request("POST", path, json_body={})
            except CoreError as exc:
                entry["status"] = "failed"
                entry["detail"] = exc.describe()
                results.append(entry)
                continue
            created_count += 1
            entry["edge_id"] = self._extract_edge_id(payload)
            if entry["edge_id"] is None:
                warnings.append(
                    "边已提交但未从响应解析出边 id（形态待现网核实）: "
                    "keys="
                    + (
                        str(sorted(payload))
                        if isinstance(payload, dict)
                        else type(payload).__name__
                    )
                )
            results.append(entry)
        if not results or created_count == len(results):
            verdict = VERDICT_PASS if created_count > 0 else VERDICT_REJECT
        elif created_count > 0:
            verdict = VERDICT_RECHECK  # 部分成功＝状态不齐，需人工核对后再补
        else:
            verdict = VERDICT_REJECT
        return {
            "verdict": verdict,
            "edges": results,
            "ticket_records": "not_applicable",
            "ticket_records_note": "平台边写不落操作记录（票号黑洞，写闸门 p22 草案处理中）",
            "warnings": warnings,
            "vocab_source": rg.vocab_source_label(self._rules),
        }

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _ticket_field_guard(self, fields: dict[str, Any]) -> dict[str, Any] | None:
        """`ticket_id` is a c_records column carried by the dedicated ticket
        parameter — a payload key with that name could silently override it,
        so refuse before anything else runs."""
        if "ticket_id" in fields:
            return {
                "verdict": VERDICT_REJECT,
                "stage": "gate",
                "note": "fields 内禁带 ticket_id：票号只走独立 ticket 参数（防覆盖/防双写）",
            }
        return None

    def _read_instance_raw_or_none(self, ci_id: str) -> dict[str, Any] | None:
        """Liveness read for gates.  404 → None; every other failure
        propagates — an unavailable upstream must never masquerade as
        'card does not exist'."""
        if ci_id in self._alive_cache:
            return self._alive_cache[ci_id]
        try:
            payload = self._client.request("GET", f"/ci/{ci_id}")
        except NotFoundError:
            self._alive_cache[ci_id] = None
            return None
        maybe_ci = payload.get("ci")
        inner: dict[str, Any] = maybe_ci if isinstance(maybe_ci, dict) else payload
        self._alive_cache[ci_id] = inner
        return inner

    def _field_roster_issues(
        self, model_name: str, fields: dict[str, Any]
    ) -> tuple[dict[str, list[rg.GateIssue]], bool]:
        """Unknown-column check against the live binding roster (best effort).

        Returns issues keyed by field ('' = payload-level), plus whether the
        roster was readable at all.
        """
        try:
            known = set(self._adapter._known_fields(model_name))
        except CoreError:
            return (
                {
                    "": [
                        rg.GateIssue(
                            rg.SEVERITY_WARN,
                            "FIELD_ROSTER_UNAVAILABLE",
                            "字段名册现读失败＝字段存在性未验证（WARN 放行）",
                        )
                    ]
                },
                False,
            )
        per_field: dict[str, list[rg.GateIssue]] = {}
        for field in fields:
            if field not in known:
                per_field.setdefault(field, []).append(
                    rg.GateIssue(
                        rg.SEVERITY_REJECT,
                        "UNKNOWN_FIELD",
                        f"字段 '{field}' 不在模型 '{model_name}' 现读字段面"
                        "（c_attributes 绑定）",
                    )
                )
        return per_field, True

    def _dedup_issues(
        self, model_name: str, fields: dict[str, Any]
    ) -> tuple[list[rg.GateIssue], str | None]:
        unique_field = rg.unique_key_attr(model_name, self._rules)
        if unique_field is None or unique_field not in fields:
            return ([], unique_field)
        value = _cell_text(fields[unique_field])
        if not value.strip():
            return ([], unique_field)
        try:
            page = self._adapter.get_instances(
                model_name, page_size=50, cursor=None, filters={unique_field: value}
            )
        except CoreError as exc:
            # Fail closed: without a verified dedup read we cannot prove the
            # card is unique, and duplicates are the worst failure mode here
            # (the ops executor treated this leg as FATAL).
            return (
                [
                    rg.GateIssue(
                        rg.SEVERITY_REJECT,
                        "DEDUP_UNVERIFIED",
                        f"唯一键判重现读失败＝不开写闸：列 '{unique_field}'='{value}'"
                        f"（{exc.describe()}）",
                    )
                ],
                unique_field,
            )
        hits = [
            item.instance_id
            for item in page.items
            if _cell_text(item.data.get(unique_field)) == value
        ]
        if hits:
            return (
                [
                    rg.GateIssue(
                        rg.SEVERITY_REJECT,
                        "DUPLICATE_CARD",
                        f"唯一键 '{unique_field}'='{value}' 已在册 "
                        f"ci_id={', '.join(hits)}＝⛔ 重复建卡（先盘点再写）",
                    )
                ],
                unique_field,
            )
        return ([], unique_field)

    def _live_values_for(self, fields: dict[str, Any]) -> dict[str, tuple[str, ...]]:
        """Fetch dynamic/hybrid vocab live sources for the fields actually used."""
        out: dict[str, tuple[str, ...]] = {}
        for field in fields:
            entry = rg.vocab_entry_for_field(field, self._rules)
            if entry is None or entry.get("kind") not in ("dynamic", "hybrid"):
                continue
            values = self._dynamic_values(entry)
            if values is not None:
                out[field] = values
        return out

    def _dynamic_values(self, entry: Any) -> tuple[str, ...] | None:
        binding_raw = entry.get("choice_other_binding_json")
        if not isinstance(binding_raw, str):
            return None
        try:
            binding = json.loads(binding_raw)
        except ValueError:
            return None
        if not isinstance(binding, dict):
            return None
        source_attr_id = str(binding.get("attr_id", ""))
        type_ids = binding.get("type_ids")
        if not source_attr_id or not isinstance(type_ids, list) or not type_ids:
            return None
        source_attr_name = self._attr_name_by_id(source_attr_id)
        if source_attr_name is None:
            return None
        model_name = self._model_name_by_type_id(str(type_ids[0]))
        if model_name is None:
            return None
        try:
            page = self._adapter.get_instances(model_name, page_size=50, cursor=None, filters={})
        except CoreError:
            return None
        values = {
            _cell_text(item.data.get(source_attr_name))
            for item in page.items
            if item.data.get(source_attr_name) is not None
        }
        return tuple(sorted(values))

    def _attr_name_by_id(self, attr_id: str) -> str | None:
        flags = self._rules["rules"]["field_constraints"].get("attr_flags_live_read_face", {})
        attrs = flags.get("attrs", {}) if isinstance(flags, dict) else {}
        entry = attrs.get(attr_id)
        if isinstance(entry, dict):
            name = entry.get("attr_name")
            if isinstance(name, str):
                return name
        unique = self._rules["rules"]["field_constraints"].get("unique_attrs", {})
        if isinstance(unique, dict):
            uval = unique.get(attr_id)
            if isinstance(uval, str):
                return uval
        return None

    def _model_name_by_type_id(self, type_id: str) -> str | None:
        types = self._rules["rules"]["ci_types"].get("types", {})
        if isinstance(types, dict):
            entry = types.get(type_id)
            if isinstance(entry, dict):
                name = entry.get("name")
                if isinstance(name, str):
                    return name
        return None

    def _build_cells(
        self,
        fields: dict[str, Any],
        raw_before: dict[str, Any] | None,
        issues_by_field: dict[str, list[rg.GateIssue]],
    ) -> list[dict[str, Any]]:
        cells: list[dict[str, Any]] = []
        flags = (
            self._rules["rules"]["field_constraints"]
            .get("attr_flags_live_read_face", {})
            .get("attrs", {})
        )
        name_to_alias: dict[str, str] = {}
        if isinstance(flags, dict):
            for entry in flags.values():
                if isinstance(entry, dict) and isinstance(entry.get("attr_name"), str):
                    name_to_alias[entry["attr_name"]] = str(entry.get("alias", ""))
        payload_issues = issues_by_field.get("", [])
        if payload_issues:
            cells.append(
                {
                    "field": "",
                    "alias": "（载荷级：票号/四件套/存在性）",
                    "before_len": None,
                    "before_sha12": None,
                    "after_len": None,
                    "errs": [
                        i.to_dict() for i in payload_issues if i.severity == rg.SEVERITY_REJECT
                    ],
                    "warns": [
                        i.to_dict() for i in payload_issues if i.severity == rg.SEVERITY_WARN
                    ],
                }
            )
        for field, value in fields.items():
            issues = issues_by_field.get(field, [])
            sensitive = rg.is_sensitive(field, self._rules)
            text = _cell_text(value)
            if raw_before is not None and field in raw_before:
                before_text = _cell_text(raw_before[field])
                before_len: int | None = len(before_text)
                before_sha: str | None = rg.sha12(raw_before[field])
            else:
                before_len = None if raw_before is None else 0
                before_sha = None if raw_before is None else rg.sha12("")
            cell: dict[str, Any] = {
                "field": field,
                "alias": name_to_alias.get(field, ""),
                "before_len": before_len,
                "before_sha12": before_sha,
                "after_len": len(text),
                "errs": [i.to_dict() for i in issues if i.severity == rg.SEVERITY_REJECT],
                "warns": [i.to_dict() for i in issues if i.severity == rg.SEVERITY_WARN],
            }
            if sensitive:
                cell["after_masked"] = rg.mask_value(field, value)
            cells.append(cell)
        return cells

    def _readback_cells(self, ci_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
        raw = self._read_instance_raw_after_write(ci_id)
        if raw is None:
            return None
        ok = 0
        mismatch: list[str] = []
        for field, value in fields.items():
            current = raw.get(field)
            if _cell_text(current) == _cell_text(value):
                ok += 1
            else:
                mismatch.append(field)
        return {"ok": ok, "total": len(fields), "mismatch": mismatch}

    def _read_instance_raw_after_write(self, ci_id: str) -> dict[str, Any] | None:
        """Fresh (uncached) read for post-write verification."""
        try:
            payload = self._client.request("GET", f"/ci/{ci_id}")
        except CoreError:
            return None
        inner = payload.get("ci") if isinstance(payload, dict) and "ci" in payload else payload
        return inner if isinstance(inner, dict) else None

    def _ticket_counts(self, ci_id: str, ticket: str) -> dict[str, Any]:
        """Rulers 2/3 from GET /history/records/attribute?ci_id=… (meta rows
        carry ticket_id; hist rows are the attribute-history legs).

        ``ticket_records``  : number of record (c_records) meta rows whose
                              ticket_id equals the call's ticket.
        ``attr_hist_rows``  : number of attribute-history rows under those
                              matched records for this CI.
        ``None`` values mean *not provable* → caller must RECHECK.
        """
        try:
            payload = self._client.request("GET", _PATH_HISTORY_ATTRIBUTE, params={"ci_id": ci_id})
        except CoreError:
            return {
                "ticket_records": None,
                "attr_hist_rows": None,
                "history_error": "history/records/attribute 调用失败",
            }
        records = payload.get("records") if isinstance(payload, dict) else None
        if not isinstance(records, list):
            return {
                "ticket_records": None,
                "attr_hist_rows": None,
                "history_error": "history/records/attribute 响应形态缺 records 数组（待现网核实）",
            }
        ticket_records = 0
        attr_hist_rows = 0
        saw_ticket_field = False
        for row in records:
            if not isinstance(row, list) or not row or not isinstance(row[0], dict):
                return {
                    "ticket_records": None,
                    "attr_hist_rows": None,
                    "history_error": (
                        "history/records/attribute 响应行形态非 [meta, [hist…]]（待现网核实）"
                    ),
                }
            meta = row[0]
            hists = row[1] if len(row) > 1 and isinstance(row[1], list) else []
            if "ticket_id" in meta:
                saw_ticket_field = True
            if str(meta.get("ticket_id") or "") == ticket:
                ticket_records += 1
                attr_hist_rows += len(hists)
        if not saw_ticket_field and records:
            return {
                "ticket_records": None,
                "attr_hist_rows": None,
                "history_error": "history meta 行未见 ticket_id 字段（键名待现网核实）",
            }
        return {
            "ticket_records": ticket_records,
            "attr_hist_rows": attr_hist_rows,
            "history_error": None,
        }

    def _relation_type_map(self) -> dict[int, str]:
        try:
            payload = self._client.request_array("/relation_types")
        except CoreError:
            return {}
        return mapper.map_relation_types(payload)

    def _edge_roster_warning(
        self, f_raw: dict[str, Any] | None, s_raw: dict[str, Any] | None, rtid: int
    ) -> str | None:
        if f_raw is None or s_raw is None:
            return None
        parent = f_raw.get("ci_type")
        child = s_raw.get("ci_type")
        if not isinstance(parent, int) or not isinstance(child, int):
            return None
        edges = self._rules["rules"].get("relation_edges", {}).get("edges")
        if not isinstance(edges, list):
            return None
        for edge in edges:
            if (
                isinstance(edge, dict)
                and edge.get("parent_type") == parent
                and edge.get("child_type") == child
                and edge.get("relation_type_id") == rtid
            ):
                return None
        return (
            f"边形态 (parent ci_type={parent} → child ci_type={child}, relation_type_id={rtid}) "
            "不在 rules.relation_edges 在册表＝WARN（在册表为快照，非硬闸）"
        )

    def _precheck_create_relations(self, relations: list[dict[str, Any]] | None) -> dict[str, Any]:
        checked: list[dict[str, Any]] = []
        warnings: list[str] = []
        verdict = VERDICT_PASS
        if not relations:
            return {"verdict": verdict, "checked": checked, "warnings": warnings}
        type_map = self._relation_type_map()
        for raw in relations:
            rtid = _as_digits(raw.get("relation_type_id"), "relation_type_id")
            other = _as_digits(raw.get("second_ci_id"), "second_ci_id")
            direction = raw.get("direction", "out")
            entry: dict[str, Any] = {
                "relation_type_id": int(rtid),
                "second_ci_id": int(other),
                "direction": direction,
                "problems": [],
            }
            if direction not in ("out", "in"):
                entry["problems"].append(f"direction '{direction}' 非 'out'/'in'")
            if rtid not in {str(k) for k in type_map}:
                entry["problems"].append(f"relation_type_id={rtid} 未在册（GET /relation_types）")
            if self._read_instance_raw_or_none(other) is None:
                entry["problems"].append(f"second_ci_id={other} 不在活面")
            checked.append(entry)
            if entry["problems"]:
                verdict = VERDICT_REJECT
        return {"verdict": verdict, "checked": checked, "warnings": warnings}

    def _create_edges_for_card(
        self, ci_id: str, relations: list[dict[str, Any]] | None, ticket: str
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for raw in relations or []:
            rtid = int(str(raw.get("relation_type_id")))
            other = _as_digits(raw.get("second_ci_id"), "second_ci_id")
            direction = str(raw.get("direction", "out"))
            first, second = (ci_id, other) if direction == "out" else (other, ci_id)
            entry: dict[str, Any] = {
                "relation_type_id": rtid,
                "other_ci_id": int(other),
                "direction": direction,
                "first_ci_id": int(first),
                "second_ci_id": int(second),
                "status": "created",
                "edge_id": None,
                "detail": "",
            }
            try:
                path = _PATH_EDGE_CREATE.format(first_ci_id=first, second_ci_id=second)
                payload = self._client.request("POST", path, json_body={})
            except CoreError as exc:
                entry["status"] = "failed"
                entry["detail"] = exc.describe()
                out.append(entry)
                continue
            entry["edge_id"] = self._extract_edge_id(payload)
            out.append(entry)
        return out

    def _extract_edge_id(self, payload: Any) -> int | None:
        if not isinstance(payload, dict):
            return None
        for key in ("id", "relation_id", "ci_relation_id"):
            value = payload.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
            if isinstance(value, str) and value.isdigit():
                return int(value)
        return None

    def _create_failure_with_reverse_lookup(
        self, model_name: str, fields: dict[str, Any], exc: Exception
    ) -> CoreError:
        """POST /ci failed — the write *may* still have landed (non-atomic).

        Reverse-look up the unique key first, then raise with the finding.
        The message never suggests replaying the call.
        """
        describe = getattr(exc, "describe", None)
        head = describe() if callable(describe) else f"{type(exc).__name__}: {exc}"
        finding = "唯一键反查未完成"
        unique_field = rg.unique_key_attr(model_name, self._rules)
        if unique_field is not None and unique_field in fields:
            value = _cell_text(fields[unique_field])
            try:
                page = self._adapter.get_instances(
                    model_name, page_size=50, cursor=None, filters={unique_field: value}
                )
                hits = [
                    item.instance_id
                    for item in page.items
                    if _cell_text(item.data.get(unique_field)) == value
                ]
                finding = (
                    f"唯一键反查：命中 ci_id={', '.join(hits)}（卡可能已落库）"
                    if hits
                    else "唯一键反查：0 命中（可能未落库）"
                )
            except CoreError:
                finding = "唯一键反查失败（上游不可用）"
        message = (
            f"cmdb_create_card: POST /ci 失败＝{head}；{finding}。"
            "⛔ 盲目重放本调用（POST /ci 非事务原子，500/超时也可能已落库）——"
            "先按上面的反查结论决定下一步。"
        )
        if isinstance(exc, CoreError):
            return UpstreamServerError(message, upstream_detail=exc.upstream_detail)
        return UpstreamServerError(message)


__all__ = [
    "VERDICT_PASS",
    "VERDICT_RECHECK",
    "VERDICT_REJECT",
    "VeopsIntentOps",
]
