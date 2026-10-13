"""知识更新三件套的不变量：propose 不写库、ratify 唯一写权威库且留痕、draft 不进权威查询。"""
from __future__ import annotations

import sqlite3

from fintrace_agent.knowledge.proposals import propose, ratify, verify_independent


def _concept_entry(**over):
    entry = {
        "metric_id": "revenue",
        "cn": "营业收入",
        "aliases": ["营业收入", "营收"],
        "base_unit": "元",
        "domain": "金额",
        "caliber_hint": "",
    }
    entry.update(over)
    return entry


def test_propose_does_not_write(kb):
    before = kb.table_counts()
    p = propose(kb, kind="concept", entry=_concept_entry(), run_id="run-1", evidence=["sha256:aa"])
    after = kb.table_counts()
    assert before == after                     # 严格：一行都没变
    assert p["status"] == "draft"
    assert p["authoritative"] is False
    assert p["proposal_id"] and p["run_id"] == "run-1"
    # 未落地：新别名此时还解析不到
    got = kb.resolve_metric("营收")
    assert got is None


def test_ratify_writes_authoritative_and_provenance(kb, db_path):
    prev_row = sqlite3.connect(db_path).execute(
        "SELECT version FROM concepts WHERE metric_id='revenue'").fetchone()
    previous = int(prev_row[0])

    p = propose(kb, kind="concept", entry=_concept_entry(), run_id="run-42",
                evidence=["sha256:bb"])
    res = ratify(kb, p, approver="张审核")

    assert res["previous_version"] == previous
    assert res["version"] == previous + 1
    assert res["valid_from"]                       # 非空
    assert res["approver"] == "张审核"
    assert res["introduced_by_run"] == "run-42"

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "SELECT version,status,aliases_json FROM concepts WHERE metric_id='revenue'"
        ).fetchone()
        assert int(row["version"]) == previous + 1
        assert row["status"] == "authoritative"
        prov = con.execute(
            "SELECT * FROM provenance WHERE table_name='concepts' AND key='revenue'"
        ).fetchone()
        assert prov is not None
        assert prov["previous_version"] == str(previous)
        assert prov["new_version"] == str(previous + 1)
        assert prov["approver"] == "张审核"
        assert prov["introduced_by_run"] == "run-42"
        assert prov["valid_from"]
    finally:
        con.close()

    # 权威版生效后，新增别名可解析
    assert kb.resolve_metric("营收")["metric_id"] == "revenue"


def test_draft_not_returned_by_authoritative_queries(kb, db_path):
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "INSERT INTO concepts(metric_id,cn,aliases_json,base_unit,domain,caliber_hint,"
            "version,status) VALUES('__draft_x','草稿指标','[\"草稿别名\"]','元','金额','','1','draft')")
        con.execute(
            "INSERT INTO relations(rule_id,predicate,subject_metric,object_metric,"
            "applicable_when,rule_version,status) VALUES('__draft_rule','equals','a','b','{}','1','draft')")
        con.commit()
    finally:
        con.close()

    assert kb.resolve_metric("草稿指标") is None
    assert kb.resolve_metric("草稿别名") is None
    assert all(c["metric_id"] != "__draft_x" for c in kb.all_metrics())
    assert kb.relation_rule("__draft_rule") is None


def test_verify_independent_requires_other_cases(kb):
    p = propose(kb, kind="concept", entry=_concept_entry(), run_id="run-A", evidence=[])

    # 只有同一个 run → 不许自证
    same = verify_independent(p, ["run-A"], lambda c: True)
    assert same["verified"] is False
    assert same["cases"] == []

    # 有不同案件且全过 → 通过
    ok = verify_independent(p, ["case-1", "case-2"], lambda c: {"ok": c == "case-2"})
    assert ok["verified"] is False                       # case-1 未过

    good = verify_independent(p, ["case-1", "case-2"], lambda c: True)
    assert good["verified"] is True
    assert [c["case"] for c in good["cases"]] == ["case-1", "case-2"]


def test_ratify_unit_increments_via_provenance(kb, db_path):
    entry = {"unit": "元", "base": "元", "scale": 1.0, "precision": 2, "rounding": "ROUND_HALF_UP"}
    p = propose(kb, kind="unit", entry=entry, run_id="run-u", evidence=[])
    r1 = ratify(kb, p, approver="李审核")
    assert r1["previous_version"] == 0 and r1["version"] == 1
    r2 = ratify(kb, p, approver="李审核")
    assert r2["previous_version"] == 1 and r2["version"] == 2   # 无版本列的表靠溯源表递增
