"""知识库运行时只读查询接口（``KnowledgeBase``）。

不变量：
- 权威查询（resolve_metric / formula / relation_rule / precision_for / all_metrics）
  **只返回 status='authoritative'** 的条目；草稿（draft）不进权威查询。
- ``resolve_metric`` 对引擎 ``METRICS`` 里每一个 ``cn``、每一个别名、以及
  ``METRIC_KEY_ALIASES`` 里每一个指标键别名都必须命中。
- 返回值里带 ``caliber`` 与 ``base_unit``。
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

_FLAT_RX = re.compile(r"\s+")


def _flat(text: str) -> str:
    """去掉全部空白，与引擎 resolve_metric 的归一方式一致。"""
    return _FLAT_RX.sub("", text or "")


def _period_kind(text: str) -> str:
    """把期间字符串归一到 A / H1 / H2 / Q1..Q4（与引擎 _text_period_kind 同口径）。"""
    t = text or ""
    m = re.search(r"(?<!\d)(20\d{2})\s*年?\s*(上|下)?半年", t)
    if m:
        return f"H{'2' if m.group(2) == '下' else '1'}"
    m = re.search(r"(?<!\d)(20\d{2})\s*[Hh]([12])(?!\d)", t)      # 紧凑写法 2023H1
    if m:
        return f"H{m.group(2)}"
    m = re.search(r"(?<!\d)(20\d{2})\s*年?\s*[Qq]([1-4])(?!\d)", t)
    if m:
        return f"Q{m.group(2)}"
    m = re.search(r"(?<!\d)([1-4])\s*[Qq]\s*(20\d{2})(?!\d)", t)
    if m:
        return f"Q{m.group(1)}"
    m = re.search(r"(?<!\d)(20\d{2})\s*年?(?!\d)", t)
    return "A" if m else ""


class KnowledgeBase:
    """SQLite 知识库的只读门面。"""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._con = sqlite3.connect(str(self.db_path))
        self._con.row_factory = sqlite3.Row
        # 指标键别名（equals 关系）映射：subject_metric -> object_metric
        self._key_alias: dict[str, str] = {
            row["subject_metric"]: row["object_metric"]
            for row in self._con.execute(
                "SELECT subject_metric,object_metric FROM relations"
                " WHERE predicate='equals' AND status='authoritative'")
        }

    # -- 内部 ---------------------------------------------------------------

    def close(self) -> None:
        self._con.close()

    def _concepts(self) -> list[dict]:
        rows = self._con.execute(
            "SELECT metric_id,cn,aliases_json,base_unit,domain,caliber_hint,version,status"
            " FROM concepts WHERE status='authoritative' ORDER BY metric_id")
        return [self._concept_row(r) for r in rows]

    @staticmethod
    def _concept_row(row: sqlite3.Row) -> dict:
        return {
            "metric_id": row["metric_id"],
            "cn": row["cn"],
            "aliases": json.loads(row["aliases_json"] or "[]"),
            "base_unit": row["base_unit"],
            "domain": row["domain"],
            "caliber_hint": row["caliber_hint"],
            "caliber": row["caliber_hint"],   # 对外统一叫 caliber
            "version": row["version"],
            "status": row["status"],
        }

    def _concept(self, metric_id: str) -> dict | None:
        row = self._con.execute(
            "SELECT metric_id,cn,aliases_json,base_unit,domain,caliber_hint,version,status"
            " FROM concepts WHERE metric_id=? AND status='authoritative'",
            (metric_id,)).fetchone()
        return self._concept_row(row) if row else None

    # -- 公共接口 -----------------------------------------------------------

    def resolve_metric(self, name_or_alias: str, anchor: int | None = None) -> dict | None:
        """把任意说法归一到概念；命中失败返回 None。

        ``anchor`` 给出数值在原文中的位置时，取**离数值最近**的概念名（与引擎同规则）。
        先按概念名/别名匹配，再退一步查指标键别名（equals 关系）。
        """
        t = _flat(name_or_alias)
        if not t:
            return None
        anchor_flat = len(t) if anchor is None else len(_flat(name_or_alias[:anchor]))

        best_cand: tuple | None = None
        best_concept: dict | None = None
        for concept in self._concepts():
            for name in [*concept["aliases"], concept["cn"]]:
                na = _flat(name)
                if not na:
                    continue
                start = 0
                while True:
                    pos = t.find(na, start)
                    if pos < 0:
                        break
                    start = pos + 1
                    dist = anchor_flat - (pos + len(na))
                    if dist < 0:      # 指标名在数值之后，视为配不上
                        dist = 800.0 - dist
                    cand = (float(dist), -len(na), concept["metric_id"])
                    if best_cand is None or cand < best_cand:
                        best_cand = cand
                        best_concept = concept

        if best_concept is not None:
            return best_concept

        # 退一步：指标键别名（如 net_profit_deducted_nonrecurring → net_profit_deducted）
        for alias, canonical in self._key_alias.items():
            if t == _flat(alias):
                return self._concept(canonical)
        return None

    def formula(self, metric_id: str, caliber: str = "") -> dict | None:
        """取指标的公式定义。给了 caliber 就精确匹配，否则取第一条。"""
        if caliber:
            row = self._con.execute(
                "SELECT * FROM formulas WHERE metric_id=? AND caliber=? AND status='authoritative'"
                " ORDER BY formula_id LIMIT 1", (metric_id, caliber)).fetchone()
        else:
            row = self._con.execute(
                "SELECT * FROM formulas WHERE metric_id=? AND status='authoritative'"
                " ORDER BY caliber, formula_id LIMIT 1", (metric_id,)).fetchone()
        if not row:
            return None
        return {
            "formula_id": row["formula_id"],
            "metric_id": row["metric_id"],
            "caliber": row["caliber"],
            "expression": row["expression"],
            "inputs": json.loads(row["inputs_json"] or "[]"),
            "rounding": json.loads(row["rounding"] or "{}"),
            "applicable_when": json.loads(row["applicable_when"] or "{}"),
            "rule_version": row["rule_version"],
            "status": row["status"],
        }

    def relation_rule(self, rule_id: str) -> dict | None:
        row = self._con.execute(
            "SELECT * FROM relations WHERE rule_id=? AND status='authoritative'",
            (rule_id,)).fetchone()
        if not row:
            return None
        return {
            "rule_id": row["rule_id"],
            "predicate": row["predicate"],
            "subject_metric": row["subject_metric"],
            "object_metric": row["object_metric"],
            "applicable_when": json.loads(row["applicable_when"] or "{}"),
            "rule_version": row["rule_version"],
            "status": row["status"],
        }

    def context_check(self, entity: str = "", period: str = "", unit: str = "") -> dict:
        """三元组（主体 / 期间 / 单位）与受控词表是否一致。只查不写。"""
        kinds = self._context_values()
        checks: dict[str, dict] = {}
        if unit:
            known = sorted(kinds.get("unit", ()))
            checks["unit"] = {"value": unit, "ok": unit in known, "known": known}
        if period:
            kind = _period_kind(period)
            known = sorted(kinds.get("period_kind", ()))
            checks["period"] = {"value": period, "kind": kind, "ok": bool(kind) and kind in known}
        if entity:
            checks["entity"] = {"value": entity, "ok": bool(entity.strip())}
        ok_all = bool(checks) and all(c["ok"] for c in checks.values())
        return {"entity": entity, "period": period, "unit": unit, "ok": ok_all, "checks": checks}

    def precision_for(self, metric_id: str, unit: str) -> dict | None:
        """指标在给定单位下的精度与换算。单位不认识返回 None。"""
        row = self._con.execute(
            "SELECT unit,base,scale,precision,rounding FROM units WHERE unit=?",
            (unit,)).fetchone()
        if not row:
            return None
        return {
            "metric_id": metric_id,
            "unit": row["unit"],
            "base": row["base"],
            "scale": row["scale"],
            "precision": row["precision"],
            "rounding": row["rounding"],
        }

    def all_metrics(self) -> list[dict]:
        return self._concepts()

    # -- 辅助 ---------------------------------------------------------------

    def _context_values(self) -> dict[str, set]:
        out: dict[str, set] = {}
        for row in self._con.execute("SELECT kind,value FROM contexts"):
            out.setdefault(row["kind"], set()).add(row["value"])
        return out

    def table_counts(self) -> dict[str, int]:
        """各表行数（供测试与统计使用，只读）。"""
        out: dict[str, int] = {}
        for (name,) in self._con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
            out[name] = self._con.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        return out


__all__ = ["KnowledgeBase"]
