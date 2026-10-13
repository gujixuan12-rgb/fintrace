"""抽取保真：落盘/入库的值必须与引擎运行时对象逐一相等（不许手抄、不许漂移）。"""
from __future__ import annotations

import json
import sqlite3

from fintrace_engine import draft_review as dr
from fintrace_engine.calculators import consistency as consistency_mod

from fintrace_agent.knowledge import extract_from_engine, yamlio
from fintrace_agent.knowledge.loader import KnowledgeBase


def _units(db_path):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        return {r["unit"]: dict(r) for r in con.execute("SELECT * FROM units")}
    finally:
        con.close()


def test_units_match_engine_objects(db_path):
    units = _units(db_path)
    for unit, scale in dr.MONEY_SCALE.items():
        assert units[unit]["base"] == "元"
        assert units[unit]["scale"] == scale
        assert units[unit]["precision"] == dr.PRECISION.get("元", 2)
    for unit, scale in dr.COUNT_SCALE.items():
        assert units[unit]["base"] == "股"
        assert units[unit]["scale"] == scale
        assert units[unit]["precision"] == dr.PRECISION.get("股", 2)
    for unit in dr.PASSTHROUGH_UNITS:
        assert units[unit]["base"] == unit
        assert units[unit]["scale"] == 1.0
    # 单位集合恰好覆盖引擎的三个来源
    expected = set(dr.MONEY_SCALE) | set(dr.COUNT_SCALE) | set(dr.PASSTHROUGH_UNITS)
    assert set(units) == expected


def test_concepts_match_engine_table(kb):
    by_id = {c["metric_id"]: c for c in kb.all_metrics()}
    assert set(by_id) == set(dr.METRICS)
    for metric_id, spec in dr.METRICS.items():
        assert by_id[metric_id]["cn"] == spec["cn"]
        assert by_id[metric_id]["aliases"] == list(spec["aliases"])
        assert by_id[metric_id]["base_unit"] == spec["unit"]


def test_formula_matches_engine(kb):
    f = kb.formula("cash_dividend")
    assert f is not None
    fn = consistency_mod.expected_dividend_yuan
    # 表达式来自 docstring 首行「：…，…」之间
    doc = (fn.__doc__ or "").splitlines()[0]
    assert f["expression"] == doc.split("：", 1)[1].split("，", 1)[0]
    assert f["inputs"] == ["total_shares", "dividend_per_ten"]
    assert f["rounding"]["scale"] == 2
    assert f["rounding"]["mode"] == "ROUND_HALF_UP"
    assert f["status"] == "authoritative"


def test_yaml_roundtrip_and_idempotent(tmp_path):
    first = extract_from_engine.generate(tmp_path / "a")
    second = extract_from_engine.generate(tmp_path / "b")
    for p1, p2 in zip(first, second):
        b1 = p1.read_bytes()
        b2 = (tmp_path / "b" / p1.name).read_bytes()
        assert b1 == b2, f"{p1.name} 两次生成不一致"
        # round-trip：解析回对象应与生成时的记录一致
        doc = yamlio.loads(b1.decode("utf-8"))
        assert yamlio.dumps(doc).encode("utf-8") == b1


def test_build_db_counts(yaml_dir, tmp_path):
    counts = __import__("fintrace_agent.knowledge.build", fromlist=["build_db"]).build_db(
        yaml_dir, tmp_path / "k.sqlite")
    assert counts["concepts"] == len(dr.METRICS)
    assert counts["units"] == len(set(dr.MONEY_SCALE) | set(dr.COUNT_SCALE) | set(dr.PASSTHROUGH_UNITS))
    assert counts["formulas"] == 1
    assert counts["relations"] == len(dr.METRIC_KEY_ALIASES) + 2  # 别名等价 + 公式两个输入
