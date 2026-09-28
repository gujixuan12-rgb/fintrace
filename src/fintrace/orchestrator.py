"""八步编排：文件识别 → 截止日期过滤 → 数据提取 → 确定性计算 →
异常评分 → 风险假设 → 核查建议 → 证据引用。

一次 run() 同时产出两样东西：
  1. `records` —— 统一数据格式 v1 的 ClaimRecord 列表（给其他成员/前端用）；
  2. `report`  —— 结构化风险报告（给人看，已过措辞约束）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .calculators import ratios
from .models import ClaimRecord, EvidenceRef, RawValue, RecomputeResult, SourceDocument
from .parsers import document_recognizer as dr
from .reporting import risk_report as rrep
from .retrieval.evidence_index import EvidenceIndex
from .verification import cutoff_filter as cf
from .verification import wording_guard as wg
from .verification.anomaly_rules import Hypothesis, Thresholds, has_authoritative_confirmation, score

HYPOTHESIS_CONFIDENCE = {"high": 0.72, "medium": 0.55, "low": 0.3}
NO_EVIDENCE_PENALTY = 0.35


@dataclass
class RunResult:
    run_log_id: str
    records: list[ClaimRecord]
    report: rrep.RiskReport
    stages: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_log_id": self.run_log_id,
            "records": [r.to_dict() for r in self.records],
            "report": self.report.to_dict(),
            "stages": self.stages,
        }


# ---------------------------------------------------------------------------
# 各步骤
# ---------------------------------------------------------------------------


def _load_raw_values(indicators: list[dict]) -> list[RawValue]:
    """步骤 ③：数据提取 —— mock 阶段直接读规范化字段，不做文本抽取。"""
    out = []
    for it in indicators:
        out.append(
            RawValue(
                metric=it["metric"],
                value=float(it["value"]),
                unit=it.get("unit", ""),
                period=it.get("period", ""),
            )
        )
    return out


def _attach_evidence(values: list[RawValue], idx: EvidenceIndex, cutoff: str) -> None:
    """步骤 ⑧（前半）：给每个指标值定位页码，并回填时间可用性。

    定不到就保持 evidence=None —— 后续会降级为「当前公开信息不足以确认」。
    """
    for v in values:
        if v.metric.startswith("__"):
            continue
        hits = idx.find_value(v.metric, v.value, v.unit)
        if hits:
            ref = idx.cite(hits[:1])[0]
            cf.mark_evidence(ref, cutoff)
            v.evidence = ref


def _compute(values: list[RawValue], period: str, prev_period: str) -> dict[str, RecomputeResult]:
    """步骤 ④：确定性计算。全部走 calculators.ratios，不让模型口算。"""
    out: dict[str, RecomputeResult] = {}
    out["cash_ratio"] = ratios.operating_cash_to_revenue(values, period)
    out["ar_gap"] = ratios.receivable_growth_gap(values, prev_period, period)
    out["gm_cur"] = ratios.gross_margin(values, period)
    out["gm_prev"] = ratios.gross_margin(values, prev_period)
    return out


def _record_for(
    hyp: Hypothesis,
    *,
    company: str,
    period: str,
    calc: RecomputeResult | None,
    values: list[RawValue],
    run_log_id: str,
) -> ClaimRecord:
    """把一条风险假设落成统一数据格式 v1 的记录。"""
    evidence: list[EvidenceRef] = []
    seen: set[tuple[str, int | None]] = set()
    for v in values:
        if v.metric in hyp.metrics and v.evidence is not None:
            key = (v.evidence.source_id, v.evidence.page)
            if key in seen:
                continue
            seen.add(key)
            evidence.append(v.evidence)

    base = HYPOTHESIS_CONFIDENCE.get(hyp.severity, 0.3)
    confidence = base
    unable = None
    label = hyp.label
    if not evidence:
        confidence = round(base * NO_EVIDENCE_PENALTY, 3)
        unable = "未能在已纳入的公开材料中定位到该指标对应的原始页码，无法确认数据出处"
        label = "INSUFFICIENT_INFORMATION"
    elif hyp.label == "INSUFFICIENT_INFORMATION":
        unable = "复算输入不完整，无法给出风险信号判断"

    return ClaimRecord(
        claim_id=f"{hyp.code}",
        claim_text=hyp.title,
        subject=company,
        period=period,
        evidence=evidence,
        raw_values=[v for v in values if v.metric in hyp.metrics],
        recomputation=calc,
        label=label,
        confidence=confidence,
        unable_reason=unable,
        run_log_id=run_log_id,
    )


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def run(payload: dict, thresholds: Thresholds | None = None) -> RunResult:
    """跑完整八步。

    payload 结构见 `data/mock/mock_original_inputs.json`。
    """
    company = payload["company"]
    cutoff = payload["prediction_cutoff_date"]
    mode = payload.get("task_mode", "pre_hoc")
    period = payload.get("period", "")
    prev_period = payload.get("prev_period", "")
    stages: dict[str, Any] = {}

    run_log_id = rrep.make_run_log_id(
        {"company": company, "cutoff": cutoff, "docs": [d["source_id"] for d in payload.get("documents", [])],
         "indicators": payload.get("indicators", [])}
    )
    stages["run_log_id"] = run_log_id

    # ① 文件识别
    sources, warn_recog = dr.recognize_all(payload.get("documents", []))
    by_id: dict[str, SourceDocument] = {s.source_id: s for s in sources}
    stages["stage1_recognized"] = [
        {"source_id": s.source_id, "file_name": s.file_name,
         "source_type": s.source_type, "publication_date": s.publication_date}
        for s in sources
    ]
    warnings = list(warn_recog)

    # ② 截止日期过滤（材料级）
    allowed_ids: set[str] = set()
    excluded_docs: list[dict] = []
    for s in sources:
        verdict, reasons = cf.filter_record(
            ClaimRecord(claim_id=s.source_id, claim_text="", subject=company, period="",
                        evidence=[EvidenceRef(source_id=s.source_id, file_name=s.file_name,
                                              source_type=s.source_type,
                                              publication_date=s.publication_date)]),
            cutoff,
        )
        if verdict == cf.ELIGIBLE:
            allowed_ids.add(s.source_id)
        else:
            excluded_docs.append({"source_id": s.source_id, "file_name": s.file_name,
                                  "publication_date": s.publication_date, "reasons": reasons})
    stages["stage2_excluded_documents"] = excluded_docs
    for e in excluded_docs:
        warnings.append(f"材料 {e['file_name']}（{e['publication_date']}）晚于预警截止日 {cutoff}，已排除")

    # ③ 数据提取（只索引可用材料）
    idx = EvidenceIndex.build(
        [d for d in payload.get("documents", []) if d["source_id"] in allowed_ids], by_id
    )
    values = _load_raw_values(payload.get("indicators", []))
    _attach_evidence(values, idx, cutoff)
    stages["stage3_values"] = [
        {"metric": v.metric, "value": v.value, "unit": v.unit, "period": v.period,
         "evidence_page": v.evidence.page if v.evidence else None,
         "evidence_file": v.evidence.file_name if v.evidence else None}
        for v in values if not v.metric.startswith("__")
    ]

    # ④ 确定性计算
    calc = _compute(values, period, prev_period)
    stages["stage4_calculations"] = {k: r.result if r.result is not None else r.error
                                     for k, r in calc.items()}

    # ⑤⑥⑦ 异常评分 → 风险假设 → 核查建议
    hyps = score(
        cash_ratio=calc["cash_ratio"], ar_gap=calc["ar_gap"],
        gm_cur=calc["gm_cur"], gm_prev=calc["gm_prev"], thresholds=thresholds,
    )
    stages["stage5_hypotheses"] = [{"code": h.code, "severity": h.severity, "title": h.title} for h in hyps]

    # 生成统一格式记录 + 报告条目
    confirmation = has_authoritative_confirmation(values)
    if mode == "post_hoc" and confirmation:
        # 只有事后核查 + 存在确认文件时，才允许升级标签
        for h in hyps:
            h.label = "CONFIRMED_BY_AUTHORITATIVE_DOCUMENT"
    elif mode == "pre_hoc":
        for h in hyps:
            if h.label == "CONFIRMED_BY_AUTHORITATIVE_DOCUMENT":
                h.label = "RISK_SIGNAL_DETECTED"

    calc_by_code = {
        "CASH_CONVERSION_WEAK": calc["cash_ratio"],
        "CASH_CONVERSION_UNKNOWN": calc["cash_ratio"],
        "RECEIVABLE_GROWTH_FASTER": calc["ar_gap"],
        "GROSS_MARGIN_SWING": calc["gm_cur"],
    }
    records = [
        _record_for(h, company=company, period=period, calc=calc_by_code.get(h.code),
                    values=values, run_log_id=run_log_id)
        for h in hyps
    ]

    # ⑧ 证据引用 + 报告
    signals = []
    for h, rec in zip(hyps, records):
        s = rrep.hypothesis_to_signal(h, [rec])
        signals.append(s)

    unable = [{"claim_id": r.claim_id, "reason": r.unable_reason,
               "confidence": r.confidence} for r in records if r.unable_reason]

    report = rrep.build(
        company=company, cutoff=cutoff, mode=mode, signals=signals,
        excluded=excluded_docs, unable=unable, warnings=warnings, run_log_id=run_log_id,
    )
    # 统一格式的自检：每条记录都必须能通过措辞约束
    for rec in records:
        wg.assert_clean([rec.claim_text], mode=mode, has_confirmation=confirmation, label=rec.label)

    return RunResult(run_log_id=run_log_id, records=records, report=report, stages=stages)


def run_files(inputs_path: str | Path, thresholds: Thresholds | None = None) -> RunResult:
    payload = json.loads(Path(inputs_path).read_text(encoding="utf-8"))
    return run(payload, thresholds=thresholds)
