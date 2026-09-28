"""步骤 ②：截止日期过滤。

规则（陈翊民 2026-09-26 私聊原文）：
    每条信息必须包含 publication_date / prediction_cutoff_date /
    available_before_cutoff / source_type。
    如果 publication_date 晚于预警截止日，就不能进入预警计算。

这一层是硬门，不是提示词。任何 claim 只要引用了晚于截止日的证据，
直接在代码里被标成 NOT_ELIGIBLE，模型没有机会把它写进报告。
"""
from __future__ import annotations

from ..models import ClaimRecord, EvidenceRef, parse_date

ELIGIBLE = "ELIGIBLE"
NOT_ELIGIBLE = "NOT_ELIGIBLE"
UNKNOWN_DATE = "UNKNOWN_DATE"


def mark_evidence(ref: EvidenceRef, cutoff: str) -> str:
    """给单条证据回填 available_before_cutoff，返回判定结果。"""
    try:
        pub = parse_date(ref.publication_date)
        cut = parse_date(cutoff)
    except (ValueError, TypeError):
        ref.available_before_cutoff = None
        return UNKNOWN_DATE
    ref.available_before_cutoff = pub <= cut
    return ELIGIBLE if ref.available_before_cutoff else NOT_ELIGIBLE


def filter_record(rec: ClaimRecord, cutoff: str) -> tuple[str, list[str]]:
    """对一条 claim 做时间过滤。

    返回 (判定, 理由列表)。判定为 NOT_ELIGIBLE 的记录不得进入预警计算。
    """
    reasons: list[str] = []
    verdicts = [mark_evidence(e, cutoff) for e in rec.evidence]

    if not verdicts:
        return NOT_ELIGIBLE, ["无证据引用，无法参与预警计算"]

    if UNKNOWN_DATE in verdicts:
        reasons.append(
            "存在发布日期缺失或格式非法（YYYY-MM-DD）的证据，按不可用处理"
        )
    if NOT_ELIGIBLE in verdicts:
        late = [
            f"{e.file_name}({e.publication_date})"
            for e, v in zip(rec.evidence, verdicts)
            if v == NOT_ELIGIBLE
        ]
        reasons.append(f"证据晚于预警截止日 {cutoff}：{', '.join(late)}")

    if reasons:
        return NOT_ELIGIBLE, reasons
    return ELIGIBLE, [f"全部证据发布于 {cutoff} 或之前"]


def filter_all(records: list[ClaimRecord], cutoff: str) -> dict[str, list]:
    """批量过滤，按判定分组。"""
    out: dict[str, list] = {ELIGIBLE: [], NOT_ELIGIBLE: []}
    for rec in records:
        verdict, reasons = filter_record(rec, cutoff)
        out[verdict].append({"claim_id": rec.claim_id, "reasons": reasons})
    return out
