"""知识库更新三件套：propose / verify_independent / ratify（设计文档 §8）。

不变量的落点：
- ``propose`` **不写库**（纯内存，返回 proposal dict）；测试用「调用前后各表行数一致」钉死。
- ``verify_independent`` 必须在**与产生该知识不同的案件**上复现，禁止复用同一套自证。
- ``ratify`` 是**唯一**写权威库的入口，且必须写 ``previous_version`` / ``valid_from`` /
  ``approver`` / ``introduced_by_run``（版本字段进各表，溯源信息进 ``provenance`` 表）。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable

from ..contracts import digest

#: kind → 目标表 / 主键字段 / 版本列（None 表示该表无版本列）/ 列定义
_KINDS: dict[str, dict[str, Any]] = {
    "concept": {
        "table": "concepts", "key": "metric_id", "version_col": "version",
        "columns": ["metric_id", "cn", "aliases_json", "base_unit", "domain",
                    "caliber_hint", "version", "status"],
        "required": ["metric_id", "cn", "aliases", "base_unit"],
    },
    "unit": {
        "table": "units", "key": "unit", "version_col": None,
        "columns": ["unit", "base", "scale", "precision", "rounding"],
        "required": ["unit", "base", "scale", "precision", "rounding"],
    },
    "formula": {
        "table": "formulas", "key": "formula_id", "version_col": "rule_version",
        "columns": ["formula_id", "metric_id", "caliber", "expression", "inputs_json",
                    "rounding", "applicable_when", "rule_version", "status"],
        "required": ["formula_id", "metric_id", "expression", "inputs"],
    },
    "context": {
        "table": "contexts", "key": "context_id", "version_col": None,
        "columns": ["context_id", "kind", "value", "note"],
        "required": ["context_id", "kind", "value"],
    },
    "relation": {
        "table": "relations", "key": "rule_id", "version_col": "rule_version",
        "columns": ["rule_id", "predicate", "subject_metric", "object_metric",
                    "applicable_when", "rule_version", "status"],
        "required": ["rule_id", "predicate", "subject_metric"],
    },
}

#: 列名 → entry 里的来源键（把 *_json 列映回列表字段）
def _entry_key(column: str) -> str:
    return column[:-5] if column.endswith("_json") else column


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _cell(column: str, value: Any) -> Any:
    """列表/字典落库前转成 JSON 字符串。"""
    if column.endswith("_json") or (not isinstance(value, (str, int, float, bool, type(None)))
                                    and column in ("rounding", "applicable_when")):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


# ---------------------------------------------------------------------------
# 1) 生成 proposal（不写库）
# ---------------------------------------------------------------------------


def propose(kb, *, kind: str, entry: dict, run_id: str, evidence: list[str]) -> dict:
    """把 Agent 发现的知识变更整理成 proposal。**不触碰数据库。**

    :param kb: KnowledgeBase（本函数不使用它，保留参数以固定接口）
    :param kind: concept / unit / formula / context / relation
    :param entry: 该条内容的字段（不含 status / 版本，版本由 ratify 递增）
    :param run_id: 产生该知识的 run
    :param evidence: 支撑证据 id 列表
    """
    if kind not in _KINDS:
        raise ValueError(f"未知的知识种类: {kind!r}（可用：{sorted(_KINDS)}）")
    spec = _KINDS[kind]
    entry = dict(entry)
    missing = [f for f in spec["required"] if f not in entry]
    if missing:
        raise ValueError(f"{kind} 缺必填字段: {missing}")

    payload = {"kind": kind, "entry": entry, "run_id": run_id, "evidence": list(evidence)}
    return {
        "proposal_id": digest(payload),
        "kind": kind,
        "entry": entry,
        "run_id": run_id,
        "evidence": list(evidence),
        "status": "draft",          # 草稿档：只在本次 run 生效，不进权威库
        "authoritative": False,
        "created_at": _now(),
    }


# ---------------------------------------------------------------------------
# 2) 独立验证（必须在不同案件上复现）
# ---------------------------------------------------------------------------


def verify_independent(proposal: dict, other_cases: list[str], runner: Callable[[str], Any]) -> dict:
    """在与产生该知识**不同**的案件上复现。

    :param other_cases: 候选案件/run 列表
    :param runner: ``runner(case_id) -> bool | {"ok": bool, ...}`` 的复现函数
    返回 ``{"proposal_id", "verified", "cases", "note"}``；
    没有可比对的**不同**案件时 ``verified=False``（不许自证）。
    """
    origin = proposal.get("run_id", "")
    results: list[dict] = []
    for case in other_cases:
        if case == origin:
            continue        # 禁止复用同一套回归自证
        outcome = runner(case)
        if isinstance(outcome, dict):
            ok = bool(outcome.get("ok"))
            detail = {k: v for k, v in outcome.items() if k != "ok"}
        else:
            ok = bool(outcome)
            detail = {}
        results.append({"case": case, "ok": ok, "detail": detail})

    verified = bool(results) and all(r["ok"] for r in results)
    note = ""
    if not results:
        note = "没有可比对的不同案件，无法独立验证"
    elif not verified:
        note = "存在未复现的案件"
    return {
        "proposal_id": proposal.get("proposal_id", ""),
        "verified": verified,
        "cases": results,
        "note": note,
    }


# ---------------------------------------------------------------------------
# 3) 人工审批 → 写权威库（唯一入口）
# ---------------------------------------------------------------------------


def _existing_version(cur: sqlite3.Cursor, spec: dict, key: Any) -> int:
    if spec["version_col"]:
        row = cur.execute(
            f"SELECT {spec['version_col']} FROM {spec['table']} WHERE {spec['key']}=?",
            (key,)).fetchone()
        if row and row[0] not in (None, ""):
            try:
                return int(row[0])
            except (TypeError, ValueError):
                return 0
        return 0
    # 无版本列的表：从 provenance 里找上一条 new_version
    row = cur.execute(
        "SELECT MAX(CAST(new_version AS INTEGER)) FROM provenance"
        " WHERE table_name=? AND key=?", (spec["table"], str(key))).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def ratify(kb, proposal: dict, approver: str) -> dict:
    """人审通过后写权威库。**唯一**写权威库的入口。

    写：目标表行（status='authoritative'，版本递增）+ provenance 行
    （previous_version / valid_from / approver / introduced_by_run / evidence / proposal_id）。
    """
    kind = proposal.get("kind")
    if kind not in _KINDS:
        raise ValueError(f"未知的知识种类: {kind!r}")
    spec = _KINDS[kind]
    entry = proposal.get("entry") or {}
    key = entry.get(spec["key"])
    if key is None:
        raise ValueError(f"proposal 缺主键字段 {spec['key']}")

    con: sqlite3.Connection = kb._con
    cur = con.cursor()
    previous = _existing_version(cur, spec, key)
    new_version = previous + 1

    values: list[Any] = []
    for column in spec["columns"]:
        if column == spec["version_col"]:
            values.append(str(new_version))
        elif column == "status":
            values.append("authoritative")
        else:
            values.append(_cell(column, entry.get(_entry_key(column), entry.get(column))))

    placeholders = ",".join("?" for _ in spec["columns"])
    cur.execute(
        f"INSERT OR REPLACE INTO {spec['table']}({','.join(spec['columns'])})"
        f" VALUES({placeholders})", values)

    valid_from = _today()
    cur.execute(
        "INSERT OR REPLACE INTO provenance"
        "(table_name,key,previous_version,new_version,valid_from,approver,"
        " introduced_by_run,evidence_json,proposal_id,at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (spec["table"], str(key), str(previous), str(new_version), valid_from, approver,
         proposal.get("run_id", ""), json.dumps(proposal.get("evidence", []), ensure_ascii=False),
         proposal.get("proposal_id", ""), _now()))
    con.commit()

    return {
        "table": spec["table"],
        "key": key,
        "status": "authoritative",
        "version": new_version,
        "previous_version": previous,
        "valid_from": valid_from,
        "approver": approver,
        "introduced_by_run": proposal.get("run_id", ""),
        "proposal_id": proposal.get("proposal_id", ""),
    }


__all__ = ["propose", "verify_independent", "ratify"]
