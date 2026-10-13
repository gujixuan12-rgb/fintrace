"""fintrace 统一数据模型 —— 实现 `schemas/claim_record.schema.json`（统一数据格式 v1）。

设计原则：任何成员（财务、研报、建模）产出的数据，只要符合 `ClaimRecord`，
就能直接进入系统。系统不关心一条记录是人手写的、脚本算出来的，还是模型生成的。

验收口径（陈翊民 2026-09-25 截图）：
    「其他成员提交的数据能够按同一格式进入系统。」
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Optional

# ---------------------------------------------------------------------------
# 枚举 / 常量
# ---------------------------------------------------------------------------

#: 材料来源类型。pre_hoc 预警只允许使用公开可得的一手材料。
SOURCE_TYPES = {
    "original_annual_report": "定期报告（年报/半年报/季报）",
    "original_interim_report": "临时公告",
    "inquiry_letter_reply": "监管问询函及回复",
    "audit_report": "审计报告",
    "research_report": "券商/第三方研报",
    "announcement": "公司公告",
    "industry_data": "行业数据",
    "correction_announcement": "更正公告（仅事后核查可用）",
}

#: 任务模式。事前预警是本项目的主方向。
TASK_MODES = ("pre_hoc", "post_hoc")


# ---------------------------------------------------------------------------
# 统一数据格式 v1
# ---------------------------------------------------------------------------


@dataclass
class EvidenceRef:
    """证据引用 —— 「证据文件与页码」+ 时间过滤四字段。"""

    source_id: str
    file_name: str
    source_type: str
    publication_date: str          # YYYY-MM-DD
    page: Optional[int] = None     # 页码，1-based；无法定位时为 None
    quoted_text: str = ""          # 证据原文片段
    # 由 verification.cutoff_filter 计算并回填，不要求调用方自己算
    available_before_cutoff: Optional[bool] = None


@dataclass
class RawValue:
    """「原始数值和单位」—— 直接抄自材料，未经加工。"""

    metric: str        # 指标名，如「营业收入」
    value: float
    unit: str          # 如「元」「万元」「%」
    period: str        # 报告期，如「2020A」「2020Q3」
    evidence: Optional[EvidenceRef] = None


@dataclass
class RecomputeResult:
    """「复算结果」—— 由 Python 确定性计算得出，不由模型口算。"""

    formula: str                   # 如「营业利润 = 营业收入 - 营业成本 - 期间费用」
    inputs: dict[str, float] = field(default_factory=dict)
    result: Optional[float] = None
    expected: Optional[float] = None
    unit: str = ""
    matched: Optional[bool] = None   # 复算值 vs 材料披露值是否一致
    deviation_pct: Optional[float] = None
    error: Optional[str] = None      # 输入缺失 / 口径不明时应写明，不许瞎算


@dataclass
class ClaimRecord:
    """统一数据格式 v1 的一条记录。"""

    claim_id: str
    claim_text: str                  # 「研报主张」
    subject: str                     # 「涉及主体」
    period: str                      # 「报告期」
    evidence: list[EvidenceRef] = field(default_factory=list)   # 「证据文件与页码」
    raw_values: list[RawValue] = field(default_factory=list)    # 「原始数值和单位」
    recomputation: Optional[RecomputeResult] = None             # 「复算结果」
    label: str = "INSUFFICIENT_INFORMATION"                     # 「核查标签」
    confidence: float = 0.0                                     # 「置信度」0.0-1.0
    unable_reason: Optional[str] = None                         # 「无法判断原因」
    run_log_id: str = ""                                        # 「完整运行日志编号」

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClaimRecord":
        ev = [EvidenceRef(**e) for e in d.get("evidence", [])]
        rv = [
            RawValue(**{**r, "evidence": EvidenceRef(**r["evidence"]) if r.get("evidence") else None})
            for r in d.get("raw_values", [])
        ]
        rc = RecomputeResult(**d["recomputation"]) if d.get("recomputation") else None
        return cls(
            claim_id=d["claim_id"],
            claim_text=d["claim_text"],
            subject=d["subject"],
            period=d["period"],
            evidence=ev,
            raw_values=rv,
            recomputation=rc,
            label=d.get("label", "INSUFFICIENT_INFORMATION"),
            confidence=float(d.get("confidence", 0.0)),
            unable_reason=d.get("unable_reason"),
            run_log_id=d.get("run_log_id", ""),
        )


@dataclass
class SourceDocument:
    """一份输入材料 —— 时间过滤的判定对象。"""

    source_id: str
    file_name: str
    source_type: str
    publication_date: str          # YYYY-MM-DD
    path: str = ""
    page_count: Optional[int] = None


def parse_date(s: str) -> date:
    return date.fromisoformat(s.strip()[:10])
