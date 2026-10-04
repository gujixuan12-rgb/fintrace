"""最小链路主编排入口：原始 PDF → 页码定位 → 字段结构化 → 确定性复算 → 可追溯报告。

与 `orchestrator.run()`（八步 mock 链路）并列，共用同一套约定：

- 时间过滤是硬门 —— 材料发布日期晚于预警截止日，直接拒绝进入检测（`cutoff_filter`）；
- 数字只由 Python 算 —— 全部走 `calculators.consistency`，模型没有机会口算；
- 每条发现都带证据 —— 转成统一数据格式 v1（`ClaimRecord` + `EvidenceRef`），
  落地「文件 + 页码 + 原文行 + 发布日期 + 是否在截止日之前」。

这一层只做「材料内部是否自洽」，不做成因推断、不指向责任。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from .models import ClaimRecord, EvidenceRef, RawValue, RecomputeResult
from .parsers import dividend_extract as dx
from .parsers import pdf_pages
from .reporting import dividend_report as drep
from .reporting import risk_report as rrep
from .verification import cutoff_filter as cf
from .verification import dividend_checks as dc
from .verification import wording_guard as wg

#: 观测值 → 统一数据格式里的指标名。
METRIC_NAMES = {
    "shares": "分配预案的股本基数",
    "dividend_per_ten_shares": "每10股派息数",
    "disclosed_dividend": "现金分红金额（含税）",
    "other_page_total": "利润分配总额（万元，另页披露）",
}


@dataclass
class DividendCaseResult:
    run_log_id: str
    case_id: str
    company: str
    cutoff: str
    annual_file: str
    page_count: int
    pages_hit: dict
    findings: list[dc.Finding]
    meta: dict
    records: list[ClaimRecord]
    payload: dict
    markdown: str
    notice: dict
    saved: dict = field(default_factory=dict)

    @property
    def errors(self) -> list[dc.Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warns(self) -> list[dc.Finding]:
        return [f for f in self.findings if f.severity == "warn"]

    def summary_line(self) -> str:
        if self.errors:
            line = f"发现不一致 {len(self.errors)} 处"
        else:
            line = "未发现材料内部不一致"
        if self.warns:
            line += f"，另有可疑 {len(self.warns)} 处"
        return line

    def to_dict(self) -> dict:
        return {
            "run_log_id": self.run_log_id,
            "case_id": self.case_id,
            "company": self.company,
            "annual_file": self.annual_file,
            "page_count": self.page_count,
            "conclusion": self.payload["conclusion"],
            "recalculation": self.payload["recalculation"],
            "findings": [f.to_dict() for f in self.findings],
            "records": [r.to_dict() for r in self.records],
            "notice_check": self.notice,
            "saved": self.saved,
        }


def _evidence_ref(f: dc.Field, *, source_id: str, source_type: str, publication_date: str, cutoff: str) -> EvidenceRef:
    ref = EvidenceRef(
        source_id=source_id,
        file_name=f.source_file,
        source_type=source_type,
        publication_date=publication_date,
        page=f.page,
        quoted_text=f.label_line or f.value_line,
    )
    cf.mark_evidence(ref, cutoff)
    return ref


def run_pdf_case(
    annual_path: str | Path,
    *,
    cutoff_date: str,
    publication_date: str,
    company: str = "",
    case_id: str = "",
    source_id: str = "",
    source_type: str = "original_annual_report",
    notice_path: str | Path | None = None,
    outdir: str | Path | None = None,
) -> DividendCaseResult:
    """核查一份年报 PDF 的内部现金分红披露是否自洽。

    Args:
        annual_path: 被核查的年报 PDF（原始披露材料）。
        cutoff_date: 预警截止日（YYYY-MM-DD）。晚于该日发布的材料一律不进检测。
        publication_date: 该材料的发布日期（YYYY-MM-DD）。
        notice_path: 官方更正公告 PDF，可选；只做事后检验，不参与判定。
        outdir: 若给出，写出 payload.json 与 报告.md。
    """
    annual_path = Path(annual_path)
    case_id = case_id or annual_path.stem
    source_id = source_id or f"SRC-{case_id}"

    # 硬门：材料本身必须先过时间过滤
    material_ref = EvidenceRef(
        source_id=source_id,
        file_name=annual_path.name,
        source_type=source_type,
        publication_date=publication_date,
    )
    verdict = cf.mark_evidence(material_ref, cutoff_date)
    if verdict != cf.ELIGIBLE:
        raise ValueError(
            f"材料 {annual_path.name}（发布 {publication_date}）不能在截止日 {cutoff_date} 前使用，"
            "拒绝进入检测输入"
        )

    pages = pdf_pages.load(annual_path)
    snap = dx.extract_dividend(pages)
    findings, meta = dc.check_dividend(snap)

    run_log_id = rrep.make_run_log_id(
        {
            "case_id": case_id,
            "company": company,
            "cutoff": cutoff_date,
            "publication_date": publication_date,
            "file": annual_path.name,
            "page_count": len(pages),
            "shares": str(meta.get("base")),
            "per10": str(meta.get("per10")),
        }
    )

    notice: dict = {"checked": False, "reason": "未提供更正公告（不影响判定）"}
    if notice_path:
        notice_pages = pdf_pages.load(notice_path)
        notice = dc.notice_ground_truth(notice_pages, meta.get("expected"))
        notice["file"] = Path(notice_path).name

    payload = drep.build_payload(
        case_id=case_id, company=company or case_id, annual_file=annual_path.name,
        page_count=len(pages), snap=snap, findings=findings, meta=meta,
        cutoff=cutoff_date, run_log_id=run_log_id, notice=notice,
    )
    markdown = drep.build_markdown(
        case_id=case_id, company=company or case_id, annual_file=annual_path.name,
        page_count=len(pages), snap=snap, findings=findings, meta=meta,
        cutoff=cutoff_date, run_log_id=run_log_id, notice=notice,
    )

    records = _to_records(
        findings, meta, snap,
        case_id=case_id, company=company or case_id,
        source_id=source_id, source_type=source_type,
        publication_date=publication_date, cutoff=cutoff_date,
        run_log_id=run_log_id,
    )

    # 措辞约束：报告里的话必须过守门人，不合规直接抛错，不生成半成品
    label = records[0].label if records else "INSUFFICIENT_INFORMATION"
    if records:
        wg.assert_clean(
            [f.title for f in findings] + [f.detail for f in findings],
            mode="pre_hoc",
            label=label,
        )

    result = DividendCaseResult(
        run_log_id=run_log_id, case_id=case_id, company=company or case_id,
        cutoff=cutoff_date, annual_file=annual_path.name, page_count=len(pages),
        pages_hit=snap.pages_hit, findings=findings, meta=meta, records=records,
        payload=payload, markdown=markdown, notice=notice,
    )
    if outdir:
        result.saved = drep.write_outputs(outdir, case_id, payload, markdown)
        result.payload["saved"] = result.saved
    return result


def _to_records(
    findings: list[dc.Finding],
    meta: dict,
    snap: dx.DividendSnapshot,
    *,
    case_id: str,
    company: str,
    source_id: str,
    source_type: str,
    publication_date: str,
    cutoff: str,
    run_log_id: str,
) -> list[ClaimRecord]:
    """把发现转成统一数据格式 v1。没有不一致、也没有可疑时不产出记录。"""
    errors = [f for f in findings if f.severity == "error"]
    warns = [f for f in findings if f.severity == "warn"]
    if not errors and not warns:
        return []

    def ref(f: dc.Field) -> EvidenceRef:
        return _evidence_ref(
            f, source_id=source_id, source_type=source_type,
            publication_date=publication_date, cutoff=cutoff,
        )

    evidence: list[EvidenceRef] = []
    seen: set[tuple[str, int | None]] = set()
    for fld in (snap.base, snap.per10, snap.cash_amount, snap.cash_total, snap.narr_total_wan):
        if not fld.ok:
            continue
        r = ref(fld)
        key = (r.source_id, r.page)
        if key in seen:
            continue
        seen.add(key)
        evidence.append(r)

    raw_values: list[RawValue] = []
    for key, fld in (
        ("shares", snap.base),
        ("dividend_per_ten_shares", snap.per10),
        ("disclosed_dividend", snap.cash_amount),
        ("other_page_total", snap.narr_total_wan),
    ):
        if not fld.ok:
            continue
        raw_values.append(
            RawValue(
                metric=METRIC_NAMES[key],
                value=float(fld.value),
                unit="万元" if key == "other_page_total" else "元",
                period="",
                evidence=ref(fld),
            )
        )

    expected: Decimal | None = meta.get("expected")
    disclosed_f = snap.cash_amount
    recomputation = RecomputeResult(
        formula="现金分红金额 = 股本基数 × 每10股派息数 ÷ 10（ROUND_HALF_UP 到分）",
        inputs={
            "股本基数": float(snap.base.value) if snap.base.ok else 0.0,
            "每10股派息数": float(snap.per10.value) if snap.per10.ok else 0.0,
            "披露的现金分红金额": float(disclosed_f.value) if disclosed_f.ok else 0.0,
        },
        result=float(expected) if expected is not None else None,
        expected=float(disclosed_f.value) if disclosed_f.ok else None,
        unit="元",
        matched=(expected is not None and disclosed_f.ok and expected == disclosed_f.value),
        deviation_pct=(
            float((expected - disclosed_f.value) / disclosed_f.value * 100)
            if expected is not None and disclosed_f.ok and disclosed_f.value != 0 else None
        ),
    )

    label = "RISK_SIGNAL_DETECTED" if errors else "FURTHER_VERIFICATION_SUGGESTED"
    basis = errors[0].detail if errors else warns[0].detail
    return [
        ClaimRecord(
            claim_id=f"{case_id}-DIVIDEND-01",
            claim_text=f"{company} 年报内部现金分红披露存在勾稽矛盾" if errors
            else f"{company} 年报内部现金分红披露存在可疑格式问题",
            subject=company,
            period="",
            evidence=evidence,
            raw_values=raw_values,
            recomputation=recomputation,
            label=label,
            confidence=0.9 if errors else 0.5,
            unable_reason=None,
            run_log_id=run_log_id,
        )
    ]
