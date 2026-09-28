"""步骤 ⑧：结构化风险报告。

报告的任何自然语言字段出场前必须过 `wording_guard.assert_clean`。
另外报告强制包含：运行日志编号、证据引用、无法判断原因、免责边界。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from ..models import ClaimRecord
from ..verification import wording_guard as wg
from ..verification.anomaly_rules import Hypothesis


@dataclass
class ReportSection:
    heading: str
    body: list[str]


@dataclass
class RiskReport:
    run_log_id: str
    generated_at: str
    company: str
    prediction_cutoff_date: str
    task_mode: str
    scope_note: str
    signals: list[dict[str, Any]] = field(default_factory=list)
    excluded: list[dict[str, Any]] = field(default_factory=list)
    unable_to_judge: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    disclaimer: str = (
        "本报告仅基于预警截止日之前已公开的信息给出风险信号提示，"
        "不构成对任何主体的定性结论、投资建议或法律意见。"
        "所有信号均需由专业人员进一步核查后方可使用。"
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


def make_run_log_id(payload: Any, when: datetime | None = None) -> str:
    """运行日志编号：对输入取哈希，保证同一输入可复现、可追溯。"""
    when = when or datetime.now(timezone.utc)
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()
    return f"FT-{when:%Y%m%d}-{hashlib.sha256(blob).hexdigest()[:10]}"


def hypothesis_to_signal(h: Hypothesis, claims: list[ClaimRecord]) -> dict[str, Any]:
    """把一条假设转成报告条目，附上它引用的证据与统一格式记录。"""
    label_cn = wg.ALLOWED_LABELS.get(h.label, h.label)
    return {
        "code": h.code,
        "title": h.title,
        "severity": h.severity,
        "label": h.label,
        "label_cn": label_cn,
        "basis": h.basis,
        "suggested_checks": list(h.suggested_checks),
        "metrics": list(h.metrics),
        "evidence": [
            {
                "source_id": e.source_id,
                "file_name": e.file_name,
                "page": e.page,
                "publication_date": e.publication_date,
                "available_before_cutoff": e.available_before_cutoff,
                "quoted_text": e.quoted_text,
            }
            for c in claims
            for e in c.evidence
        ],
        "claim_ids": [c.claim_id for c in claims],
    }


def build(
    *,
    company: str,
    cutoff: str,
    mode: str,
    signals: list[dict[str, Any]],
    excluded: list[dict[str, Any]],
    unable: list[dict[str, Any]],
    warnings: list[str],
    run_log_id: str,
    generated_at: Optional[str] = None,
) -> RiskReport:
    scope_note = (
        "范围：仅使用发布日期不晚于预警截止日 "
        f"{cutoff} 的公开材料；截止日之后发布的信息一律排除在预警计算之外。"
    )
    rep = RiskReport(
        run_log_id=run_log_id,
        generated_at=generated_at or datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        company=company,
        prediction_cutoff_date=cutoff,
        task_mode=mode,
        scope_note=scope_note,
        signals=signals,
        excluded=excluded,
        unable_to_judge=unable,
        warnings=warnings,
    )
    # 最后一道门：报告里所有面向用户的话都要过措辞约束
    texts: list[str] = [rep.scope_note, rep.disclaimer, *(rep.warnings or [])]
    for s in signals:
        texts += [s["title"], s["basis"], *s.get("suggested_checks", [])]
    confirmation = any(
        e.get("source_type") == "inquiry_letter_reply" for s in signals for e in s.get("evidence", [])
    )
    for s in signals:
        wg.assert_clean(texts, mode=mode, has_confirmation=confirmation, label=s["label"])
    return rep
