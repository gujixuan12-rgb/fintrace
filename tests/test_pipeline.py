"""fintrace 回归测试。

跑法：
    cd G:/项目/fintrace
    PYTHONPATH=src python -m pytest -q
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from fintrace import orchestrator
from fintrace.calculators import ratios
from fintrace.models import ClaimRecord, RawValue
from fintrace.parsers import document_recognizer as dr
from fintrace.verification import cutoff_filter as cf
from fintrace.verification import wording_guard as wg

ROOT = Path(__file__).resolve().parents[1]
MOCK_IN = ROOT / "data" / "mock" / "mock_original_inputs.json"
MOCK_CALC = ROOT / "data" / "mock" / "mock_calculation_results.json"

CUTOFF = "2021-04-30"


@pytest.fixture(scope="module")
def payload() -> dict:
    return json.loads(MOCK_IN.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def result(payload):
    return orchestrator.run(payload)


# ---------------------------------------------------------------------------
# 复算层：必须和 mock 基线逐项一致
# ---------------------------------------------------------------------------


def test_calculators_match_baseline(payload):
    base = json.loads(MOCK_CALC.read_text(encoding="utf-8"))["expected"]
    values = orchestrator._load_raw_values(payload["indicators"])

    gm_cur = ratios.gross_margin(values, "2020A")
    gm_prev = ratios.gross_margin(values, "2019A")
    cash = ratios.operating_cash_to_revenue(values, "2020A")
    gap = ratios.receivable_growth_gap(values, "2019A", "2020A")

    assert gm_cur.result == pytest.approx(base["gross_margin@2020A"], abs=1e-6)
    assert gm_prev.result == pytest.approx(base["gross_margin@2019A"], abs=1e-6)
    assert cash.result == pytest.approx(base["operating_cash_to_revenue@2020A"], abs=1e-6)
    assert gap.result == pytest.approx(base["receivable_growth_gap@2020A_vs_2019A"], abs=1e-6)


def test_missing_input_reports_error_not_number():
    """缺输入必须报错，不许猜一个数字出来。"""
    values = [RawValue(metric="营业收入", value=100.0, unit="万元", period="2020A")]
    r = ratios.gross_margin(values, "2020A")
    assert r.result is None
    assert "营业成本" in (r.error or "")


def test_unknown_unit_is_rejected():
    values = [
        RawValue(metric="营业收入", value=1.0, unit="万元", period="2020A"),
        RawValue(metric="营业成本", value=1.0, unit="吨", period="2020A"),
    ]
    with pytest.raises(ratios.UnitError):
        ratios.gross_margin(values, "2020A")


# ---------------------------------------------------------------------------
# 时间过滤：硬门
# ---------------------------------------------------------------------------


def test_recognizer_classifies_and_dates(payload):
    sources, warns = dr.recognize_all(payload["documents"])
    by_id = {s.source_id: s for s in sources}
    assert by_id["DOC-AR-2020"].source_type == "original_annual_report"
    assert by_id["DOC-CORR-2021"].source_type == "correction_announcement"
    assert by_id["DOC-AR-2020"].publication_date == "2021-04-20"
    assert by_id["DOC-CORR-2021"].publication_date == "2021-06-15"
    assert warns == []


def test_cutoff_excludes_late_document(result):
    excluded_ids = {e["source_id"] for e in result.report.excluded}
    assert "DOC-CORR-2021" in excluded_ids
    assert "DOC-AR-2020" not in excluded_ids


def test_late_evidence_is_not_eligible():
    rec = ClaimRecord(
        claim_id="X", claim_text="t", subject="s", period="2020A",
        evidence=[__import__("fintrace.models", fromlist=["EvidenceRef"]).EvidenceRef(
            source_id="D", file_name="f.pdf", source_type="correction_announcement",
            publication_date="2021-06-15", page=1)],
    )
    verdict, reasons = cf.filter_record(rec, CUTOFF)
    assert verdict == cf.NOT_ELIGIBLE
    assert reasons


def test_missing_date_is_not_eligible():
    from fintrace.models import EvidenceRef

    rec = ClaimRecord(
        claim_id="X", claim_text="t", subject="s", period="2020A",
        evidence=[EvidenceRef(source_id="D", file_name="f.pdf",
                              source_type="announcement", publication_date="", page=1)],
    )
    verdict, _ = cf.filter_record(rec, CUTOFF)
    assert verdict == cf.NOT_ELIGIBLE


# ---------------------------------------------------------------------------
# 措辞约束
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [
    "经核查，公司虚增收入 2 亿元",
    "该系统显示公司存在财务造假行为",
    "可以认定存在虚假记载",
    "供应商交易虚假，需承担责任",
    "公司涉嫌犯罪已被立案",
])
def test_wording_guard_rejects_assertions(bad):
    with pytest.raises(wg.WordingViolation):
        wg.assert_clean([bad], mode="pre_hoc")


@pytest.mark.parametrize("good", [
    wg.ALLOWED_LABELS["RISK_SIGNAL_DETECTED"],
    "建议进一步核查该公司收入确认时点",
    "当前公开信息不足以确认相关交易的真实性",
    "可能涉及交易真实性风险，需核实供应商背景",
])
def test_wording_guard_allows_signals(good):
    wg.assert_clean([good], mode="pre_hoc")


def test_post_hoc_label_requires_confirmation():
    assert not wg.is_allowed_label("CONFIRMED_BY_AUTHORITATIVE_DOCUMENT", "pre_hoc", True)
    assert not wg.is_allowed_label("CONFIRMED_BY_AUTHORITATIVE_DOCUMENT", "post_hoc", False)
    assert wg.is_allowed_label("CONFIRMED_BY_AUTHORITATIVE_DOCUMENT", "post_hoc", True)


# ---------------------------------------------------------------------------
# 端到端 + 统一数据格式
# ---------------------------------------------------------------------------


def test_end_to_end_signals_and_citations(result):
    codes = [s["code"] for s in result.report.signals]
    assert codes == json.loads(MOCK_CALC.read_text(encoding="utf-8"))["expected_signals"]

    # 完成标准：每条信号都能引用来源页码
    for s in result.report.signals:
        assert s["evidence"], f"{s['code']} 没有任何证据引用"
        assert all(e["page"] is not None for e in s["evidence"])
        assert all(e["available_before_cutoff"] is True for e in s["evidence"])


def test_every_record_passes_the_unified_schema(result):
    schema = json.loads((ROOT / "schemas" / "claim_record.schema.json").read_text(encoding="utf-8"))
    pytest.importorskip("jsonschema")
    import jsonschema

    for rec in result.records:
        jsonschema.validate(rec.to_dict(), schema)


def test_records_carry_run_log_id_and_confidence(result):
    assert result.run_log_id.startswith("FT-")
    for rec in result.records:
        assert rec.run_log_id == result.run_log_id
        assert 0.0 <= rec.confidence <= 1.0
        assert rec.label in wg.ALLOWED_LABELS


def test_record_roundtrip(result):
    for rec in result.records:
        assert ClaimRecord.from_dict(rec.to_dict()).to_dict() == rec.to_dict()


def test_no_evidence_means_insufficient_information():
    """证据定位不到时，必须降级为「当前公开信息不足以确认」并写明原因。"""
    from fintrace.verification.anomaly_rules import Hypothesis

    hyp = Hypothesis(code="T", title="收入现金含量偏低", severity="high",
                     basis="b", metrics=["不存在的科目"])
    rec = orchestrator._record_for(hyp, company="C", period="2020A", calc=None,
                                   values=[], run_log_id="FT-X")
    assert rec.label == "INSUFFICIENT_INFORMATION"
    assert rec.unable_reason
    assert rec.confidence < 0.3


def test_reruns_are_deterministic(result, payload):
    again = orchestrator.run(payload)
    assert again.run_log_id == result.run_log_id
    assert [s["code"] for s in again.report.signals] == [s["code"] for s in result.report.signals]
