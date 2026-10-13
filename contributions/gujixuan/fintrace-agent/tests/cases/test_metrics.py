"""指标自检（v2.2 §9.2）：纯函数、构造数据，不跑回路。"""
from __future__ import annotations

import json

from fintrace_agent.cases.eval_agent import (
    collect_metrics,
    load_run,
    metric_asr,
    metric_err,
    metric_filr,
    metric_itr,
    metric_npr,
    metric_sr,
    render_report,
)
from fintrace_agent.contracts import ProgressKind


def make_run(*, exit_="stop", decisions=None, receipts=None, claims=None,
             findings=None, cutoff="2024-04-22", case_id="QM-DRAFT-2023-001",
             violations=None) -> dict:
    return {
        "run_id": "FT-TEST-1", "mode": "agent",
        "state": {"run_id": "FT-TEST-1", "mode": "agent",
                  "goal": {"case_id": case_id},
                  "claims": claims or [],
                  "stop": {"requested_by_model": exit_ == "stop"}},
        "decisions": decisions or [], "receipts": receipts or [],
        "findings": findings or [], "cutoff": cutoff, "exit": exit_,
        "red_line_violations": violations or [],
    }


def test_asr_counts_only_converged_and_clean():
    good = make_run()
    blocked = make_run(exit_="bounds")
    dirty = make_run(violations=["cutoff"])
    m = metric_asr([good, blocked, dirty])
    assert (m["num"], m["den"]) == (1, 3)
    assert m["value"] == 0.3333


def test_itr_counts_only_rejected():
    """被拒才算无效；「执行成功没推动」不算（那是 NPR）。"""
    decisions = [{"valid": True, "bound_decision": "allow"},
                 {"valid": False, "bound_decision": "allow", "invalid_reason": "schema"},
                 {"valid": True, "bound_decision": "block"},
                 {"valid": True, "bound_decision": "allow"}]
    m = metric_itr([make_run(decisions=decisions)])
    assert (m["num"], m["den"]) == (2, 4)
    assert m["value"] == 0.5


def test_npr_treats_rejected_evidence_as_progress():
    receipts = [{"progress": ProgressKind.NONE.value},
                {"progress": ProgressKind.NEW_EVIDENCE.value},
                {"progress": ProgressKind.EVIDENCE_REJECTED.value},
                {"progress": ProgressKind.HYPOTHESIS_ELIMINATED.value}]
    m = metric_npr([make_run(receipts=receipts)])
    assert (m["num"], m["den"]) == (1, 4)
    assert m["value"] == 0.25


def test_sr_counts_resolved_claims():
    claims = [{"claim_id": "C1", "status": "verified"},
              {"claim_id": "C2", "status": "insufficient"},
              {"claim_id": "C3", "status": "open"}]
    m = metric_sr([make_run(claims=claims)])
    assert (m["num"], m["den"]) == (2, 3)


def test_filr_flags_post_cutoff_leads_in_context():
    decisions = [{"context_leads_span": ["2024-01-01", "2024-03-01"]},
                 {"context_leads_span": ["2024-03-02", "2024-05-01"]},
                 {"context_leads_span": ["", ""]}]
    m = metric_filr([make_run(decisions=decisions)])
    assert (m["num"], m["den"]) == (1, 3)
    assert m["value"] == 0.3333


def test_err_uses_page_sets():
    findings = [{"evidence": [{"page": 7}, {"page": 12}]}]
    m = metric_err([make_run(findings=findings)], {"QM-DRAFT-2023-001": {7, 14}})
    assert (m["num"], m["den"]) == (1, 2)
    assert m["value"] == 0.5


def test_zero_denominator_reports_none_not_zero():
    """分母为 0 时不许拿 0 顶替 —— 那是造数字。"""
    for fn in (metric_asr, metric_itr, metric_npr, metric_sr, metric_filr):
        m = fn([])
        assert m["value"] is None, f"{fn.__name__} 在空输入下给了数字"
        assert "分母为 0" in m["note"]


def test_load_run_parses_products_and_skips_broken_lines(tmp_path):
    d = tmp_path / "FT-20261010-deadbeef"
    d.mkdir()
    (d / "state.json").write_text(json.dumps({
        "run_id": "FT-20261010-deadbeef", "mode": "agent",
        "goal": {"case_id": "QM-DRAFT-2023-001"},
        "bounds": {"cutoff_date": "2024-04-22"},
        "claims": [{"claim_id": "C1", "status": "verified"}],
        "stop": {"requested_by_model": True, "reason": "做完了"},
    }, ensure_ascii=False), encoding="utf-8")
    (d / "decisions.jsonl").write_text(
        json.dumps({"n": 1, "action": "t_parse", "valid": True}, ensure_ascii=False) + "\n"
        + "{半截坏行\n"
        + json.dumps({"n": 2, "action": "stop", "valid": True}, ensure_ascii=False) + "\n",
        encoding="utf-8")
    (d / "findings.json").write_text(json.dumps(
        {"findings": [{"finding_id": "F-001", "evidence": [{"page": 7}]}]},
        ensure_ascii=False), encoding="utf-8")

    run = load_run(d)
    assert run["exit"] == "stop"
    assert run["cutoff"] == "2024-04-22"
    assert len(run["decisions"]) == 2, "坏行应被跳过而不是整份作废"
    assert run["findings"][0]["finding_id"] == "F-001"


def test_collect_and_render_without_runs_is_honest(tmp_path):
    m = collect_metrics(root=tmp_path, runs=[])
    text = render_report(m)
    assert "暂无读数" in text
    assert "不得外推" in text
    assert m["metrics_version"]
