"""把分红核查结果落成两样东西：机器可读的 payload 和能给人看的报告 markdown。

报告里每个数字都带出处（文件 + 页码 + 原文行）；结论只落在「材料内部是否自洽」，
边界写死在报告尾部，不判断成因、不指向责任。
"""
from __future__ import annotations

import json
from pathlib import Path

from ..parsers.dividend_extract import DividendSnapshot
from ..verification.dividend_checks import Finding

_SEV_LABEL = {"error": "不一致", "warn": "可疑", "info": "说明"}

BOUNDARY = "仅识别披露材料内部的勾稽矛盾，不判断成因或责任，不构成对任何主体的定性结论。"

DISCLAIMER = (
    "本报告仅基于预警截止日之前已公开的材料给出风险信号提示，"
    "不构成对任何主体的定性结论、投资建议或法律意见。"
    "所有信号均需由专业人员进一步核查后方可使用。"
)


def _money(value) -> str:
    return f"{value:,}" if value is not None else "未取到"


def build_payload(
    *,
    case_id: str,
    company: str,
    annual_file: str,
    page_count: int,
    snap: DividendSnapshot,
    findings: list[Finding],
    meta: dict,
    cutoff: str,
    run_log_id: str,
    notice: dict | None = None,
) -> dict:
    """机器可读结果：结论、复算过程、逐条发现（含证据页码与原文行）。"""
    errors = [f for f in findings if f.severity == "error"]
    warns = [f for f in findings if f.severity == "warn"]
    return {
        "run_log_id": run_log_id,
        "case_id": case_id,
        "company": company,
        "prediction_cutoff_date": cutoff,
        "annual_file": annual_file,
        "page_count": page_count,
        "pages_hit": snap.pages_hit,
        "conclusion": {
            "status": "REVIEW_REQUIRED" if errors else "CONSISTENT",
            "error": len(errors),
            "warn": len(warns),
            "info": len(findings) - len(errors) - len(warns),
        },
        "recalculation": {
            "formula": meta.get("formula"),
            "shares": str(meta.get("base")) if meta.get("base") is not None else None,
            "shares_source": meta.get("base_source"),
            "dividend_per_ten_shares": str(meta.get("per10")) if meta.get("per10") is not None else None,
            "dividend_per_ten_shares_source": meta.get("per10_source"),
            "expected_yuan": str(meta.get("expected")) if meta.get("expected") is not None else None,
        },
        "findings": [f.to_dict() for f in findings],
        "notice_check": notice or {"checked": False, "reason": "未提供更正公告（不影响判定）"},
        "boundary": BOUNDARY,
    }


def build_markdown(
    *,
    case_id: str,
    company: str,
    annual_file: str,
    page_count: int,
    snap: DividendSnapshot,
    findings: list[Finding],
    meta: dict,
    cutoff: str,
    run_log_id: str,
    notice: dict | None = None,
) -> str:
    errors = [f for f in findings if f.severity == "error"]
    warns = [f for f in findings if f.severity == "warn"]
    hits = "、".join(f"{k} 第 {v} 页" for k, v in snap.pages_hit.items() if v)

    out: list[str] = []
    out.append(f"# 披露材料内部核查报告 · {company or case_id}")
    out.append("")
    out.append(f"- 运行日志编号：`{run_log_id}`")
    out.append(f"- 被核查材料：`{annual_file}`（共 {page_count} 页）")
    out.append(f"- 预警截止日：{cutoff}")
    out.append(f"- 页码定位命中：{hits or '无'}")
    out.append("- 核查口径：现金分红（股本基数 × 每 10 股派息数 ÷ 10，Decimal 复算，ROUND_HALF_UP）")
    conclusion = f"发现不一致 {len(errors)} 处" if errors else "未发现材料内部不一致"
    if warns:
        conclusion += f"，另有 {len(warns)} 处可疑（格式类）"
    out.append(f"- 结论：**{conclusion}**")
    out.append("")
    out.append("## 一、复算过程（可复现）")
    out.append("")
    out.append("| 项 | 取值 | 出处 |")
    out.append("|---|---|---|")
    out.append(f"| 股本基数 | {_money(meta.get('base'))} | {meta.get('base_source') or '未取到'} |")
    out.append(f"| 每 10 股派息数 | {meta.get('per10') if meta.get('per10') is not None else '未取到'} | {meta.get('per10_source') or '未取到'} |")
    out.append(f"| 复算值 | {_money(meta.get('expected'))} 元 | {meta.get('formula')} |")
    out.append("")
    out.append("## 二、发现清单")
    out.append("")
    if not findings:
        out.append("本次未产出任何发现。")
    for f in findings:
        out.append(f"### [{_SEV_LABEL.get(f.severity, f.severity)}] {f.code}　{f.title}")
        out.append("")
        out.append(f.detail)
        out.append("")
        if f.evidence:
            out.append("证据：")
            out.append("")
            for e in f.evidence:
                val = f"　值 = {e.value}" if e.value is not None else ""
                page = f"第 {e.page} 页" if e.page else "页码未取到"
                out.append(f"- `{e.file}` {page}：{e.line}{val}")
            out.append("")
    if notice:
        out.append("## 三、与官方更正公告的事后比对")
        out.append("")
        if notice.get("hit"):
            out.append(
                f"- 复算值 **{notice['value']}** 出现在 `{notice['file']}` 第 {notice['page']} 页"
                "（字面命中）。该公告发布日期晚于预警截止日，**未参与本链路判定**，仅用于事后检验检出效果。"
            )
        elif notice.get("checked"):
            out.append(f"- 已在 `{notice.get('file', '公告')}` 中检索复算值，未字面命中：{notice.get('reason', '')}")
        else:
            out.append(f"- {notice.get('reason', '未提供更正公告')}")
        out.append("")
    out.append("## 四、边界")
    out.append("")
    out.append(DISCLAIMER)
    out.append("")
    out.append(BOUNDARY)
    out.append("")
    return "\n".join(out)


def write_outputs(outdir: str | Path, name: str, payload: dict, markdown: str) -> dict:
    """写出 `<outdir>/payload.json` 与 `<outdir>/报告.md`，返回落盘路径。"""
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    payload_path = outdir / "payload.json"
    report_path = outdir / "报告.md"
    payload_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path.write_text(markdown, encoding="utf-8")
    return {"payload": str(payload_path), "report": str(report_path)}
