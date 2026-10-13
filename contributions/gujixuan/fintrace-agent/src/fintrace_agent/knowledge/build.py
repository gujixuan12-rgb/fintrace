"""把 ``knowledge/data/*.yaml`` 编译进 SQLite 知识库。

``build_db`` 是**全量重建**（DROP→CREATE→重灌），因此可重复执行且结果确定；
``proposals.ratify`` 之后新增的权威版本会在下次重建时被这里的数据覆盖 —— 这是刻意的：
数据文件是权威版的来源，ratify 的增量留痕在 ``provenance`` 表与数据文件导出里。

表结构严格按 BUILD_SPEC 第 2 节列出的列；额外的版本溯源信息放在单独的 ``provenance``
表里，避免改动规格列。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from . import yamlio

#: 五张知识表 + 一张溯源表。列顺序即 BUILD_SPEC 第 2 节的列定义。
DDL: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS concepts(
        metric_id TEXT PRIMARY KEY,
        cn TEXT NOT NULL,
        aliases_json TEXT NOT NULL,
        base_unit TEXT NOT NULL,
        domain TEXT NOT NULL,
        caliber_hint TEXT NOT NULL DEFAULT '',
        version TEXT NOT NULL DEFAULT '1',
        status TEXT NOT NULL DEFAULT 'authoritative')""",
    """CREATE TABLE IF NOT EXISTS units(
        unit TEXT PRIMARY KEY,
        base TEXT NOT NULL,
        scale REAL NOT NULL,
        precision INTEGER NOT NULL,
        rounding TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS formulas(
        formula_id TEXT PRIMARY KEY,
        metric_id TEXT NOT NULL,
        caliber TEXT NOT NULL DEFAULT '',
        expression TEXT NOT NULL,
        inputs_json TEXT NOT NULL,
        rounding TEXT NOT NULL,
        applicable_when TEXT NOT NULL,
        rule_version TEXT NOT NULL DEFAULT '1',
        status TEXT NOT NULL DEFAULT 'authoritative')""",
    """CREATE TABLE IF NOT EXISTS contexts(
        context_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        value TEXT NOT NULL,
        note TEXT NOT NULL DEFAULT '')""",
    """CREATE TABLE IF NOT EXISTS relations(
        rule_id TEXT PRIMARY KEY,
        predicate TEXT NOT NULL,
        subject_metric TEXT NOT NULL,
        object_metric TEXT NOT NULL DEFAULT '',
        applicable_when TEXT NOT NULL,
        rule_version TEXT NOT NULL DEFAULT '1',
        status TEXT NOT NULL DEFAULT 'authoritative')""",
    # 溯源表：ratify 写入，记录 previous_version/valid_from/approver/introduced_by_run
    """CREATE TABLE IF NOT EXISTS provenance(
        table_name TEXT NOT NULL,
        key TEXT NOT NULL,
        previous_version TEXT NOT NULL,
        new_version TEXT NOT NULL,
        valid_from TEXT NOT NULL,
        approver TEXT NOT NULL,
        introduced_by_run TEXT NOT NULL,
        evidence_json TEXT NOT NULL DEFAULT '[]',
        proposal_id TEXT NOT NULL DEFAULT '',
        at TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (table_name, key, new_version))""",
)

#: 全量重建时要清空的表（顺序无关）
_TABLES = ("concepts", "units", "formulas", "contexts", "relations", "provenance")


def _load_records(yaml_dir: Path, name: str) -> list[dict]:
    path = yaml_dir / f"{name}.yaml"
    text = path.read_text(encoding="utf-8")
    doc = yamlio.loads(text)
    if not isinstance(doc, dict) or not isinstance(doc.get("records"), list):
        raise ValueError(f"{path} 结构不对：缺少 records 列表")
    return doc["records"]


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def build_db(yaml_dir: str | Path, db_path: str | Path) -> dict:
    """从 ``yaml_dir`` 编译到 ``db_path``，返回各表行数。"""
    yaml_dir = Path(yaml_dir)
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    concepts = _load_records(yaml_dir, "concepts")
    units = _load_records(yaml_dir, "units")
    formulas = _load_records(yaml_dir, "formulas")
    contexts = _load_records(yaml_dir, "contexts")
    relations = _load_records(yaml_dir, "relations")

    con = sqlite3.connect(db_path)
    try:
        cur = con.cursor()
        for stmt in DDL:
            cur.execute(stmt)
        for table in _TABLES:
            cur.execute(f"DELETE FROM {table}")

        cur.executemany(
            "INSERT OR REPLACE INTO concepts"
            "(metric_id,cn,aliases_json,base_unit,domain,caliber_hint,version,status)"
            " VALUES(?,?,?,?,?,?,?,?)",
            [(r["metric_id"], r["cn"], _json(r["aliases"]), r["base_unit"], r["domain"],
              r.get("caliber_hint", ""), r.get("version", "1"),
              r.get("status", "authoritative")) for r in concepts])

        cur.executemany(
            "INSERT OR REPLACE INTO units(unit,base,scale,precision,rounding)"
            " VALUES(?,?,?,?,?)",
            [(r["unit"], r["base"], float(r["scale"]), int(r["precision"]),
              r["rounding"]) for r in units])

        cur.executemany(
            "INSERT OR REPLACE INTO formulas"
            "(formula_id,metric_id,caliber,expression,inputs_json,rounding,"
            " applicable_when,rule_version,status) VALUES(?,?,?,?,?,?,?,?,?)",
            [(r["formula_id"], r["metric_id"], r.get("caliber", ""), r["expression"],
              _json(r["inputs"]), _json(r["rounding"]), _json(r["applicable_when"]),
              r.get("rule_version", "1"), r.get("status", "authoritative"))
             for r in formulas])

        cur.executemany(
            "INSERT OR REPLACE INTO contexts(context_id,kind,value,note) VALUES(?,?,?,?)",
            [(r["context_id"], r["kind"], r["value"], r.get("note", "")) for r in contexts])

        cur.executemany(
            "INSERT OR REPLACE INTO relations"
            "(rule_id,predicate,subject_metric,object_metric,applicable_when,"
            " rule_version,status) VALUES(?,?,?,?,?,?,?)",
            [(r["rule_id"], r["predicate"], r["subject_metric"], r.get("object_metric", ""),
              _json(r["applicable_when"]), r.get("rule_version", "1"),
              r.get("status", "authoritative")) for r in relations])

        con.commit()
    finally:
        con.close()

    return {
        "concepts": len(concepts),
        "units": len(units),
        "formulas": len(formulas),
        "contexts": len(contexts),
        "relations": len(relations),
        "provenance": 0,
    }


__all__ = ["build_db", "DDL"]
