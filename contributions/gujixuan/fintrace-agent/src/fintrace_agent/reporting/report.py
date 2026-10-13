"""报告层（v2.2 §7 V6）：`findings.json` + `report.md`。

两条硬要求写在这里，不靠记性：
1. **必须带未解决清单**：只报解决的部分等于隐瞒；残余项（未解疑点、失败工具、待人工复核）一律列出。
2. **一手证据与二手线索分开呈现**：二手线索不得单独支撑矛盾结论。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from fintrace_agent.contracts import Finding, Residual, TaskState

DISCLAIMER = ("本系统只输出风险信号，不做定性结论。报告中任何输出都需经专业人员"
              "进一步核查后方可使用。")


def guard_text(text: str) -> None:
    """措辞守门人：命中定性断言（造假/虚增一类）就抛异常，不生成半成品报告。"""
    from fintrace_engine.verification import wording_guard as wg

    fn = (getattr(wg, "assert_clean", None) or getattr(wg, "check", None)
          or getattr(wg, "enforce", None) or getattr(wg, "scan", None))
    if fn is None:                                    # 找不到入口就报错，不许静默跳过
        raise RuntimeError("wording_guard 没有可用的检查入口，措辞守门人无法执行")
    fn(text)


def _claims_view(state: TaskState | None) -> list[dict]:
    if state is None:
        return []
    out = []
    for c in state.claims or []:
        out.append({"claim_id": c.claim_id, "status": c.status,
                    "open_questions": list(c.open_questions or []),
                    "blocked_reason": c.blocked_reason})
    return out


def findings_payload(findings: list[Finding], state: TaskState | None = None,
                     *, gate=None, mode: str = "", extra: dict | None = None) -> dict:
    """组装可落盘的 payload。gate 传 GateResult 时会带上发布决定与残余清单。"""
    gate_counts = {}
    residuals: list[Residual] = []
    if gate is not None:
        gate_counts = dict(getattr(gate, "counts", {}) or {})
        residuals = list(getattr(gate, "residuals", []) or [])

    counts: dict[str, int] = {}
    for f in findings:
        counts[f.verdict] = counts.get(f.verdict, 0) + 1

    payload = {
        "run_id": getattr(state, "run_id", "") if state else "",
        "mode": mode or (getattr(state, "mode", "") if state else ""),
        "case": dict(getattr(state, "goal", {}) or {}) if state else {},
        "cutoff": (state.bounds.cutoff_date if state else ""),
        "as_of_mode": (state.bounds.as_of_mode if state else ""),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "counts": counts,
        "gate_counts": gate_counts,
        "findings": [f.to_dict() for f in findings],
        "residuals": [r.to_dict() for r in residuals],
        "claims": _claims_view(state),
    }
    if extra:
        payload.update(extra)
    return payload


def render_markdown(payload: dict) -> str:
    """渲染人读的报告。逐条 Finding + 未解决清单 + 免责声明。"""
    findings = payload.get("findings") or []
    residuals = payload.get("residuals") or []
    case = payload.get("case") or {}
    L: list[str] = []
    L.append(f"# FinTrace 核查报告 · {case.get('case_id', '未命名案件')}")
    L.append("")
    L.append(f"- 运行编号：`{payload.get('run_id', '')}`　模式：{payload.get('mode', '')}")
    L.append(f"- 公司/报告期：{case.get('company', '')} {case.get('period', '')}"
             f"　截止日：{payload.get('cutoff', '')}（{payload.get('as_of_mode', '')}）")
    L.append(f"- 生成时间：{payload.get('generated_at', '')}")
    L.append("")

    counts = payload.get("counts") or {}
    L.append("## 一、结论摘要")
    L.append("")
    L.append(f"- 共 {len(findings)} 条核查结论："
             + "、".join(f"{k} {v}" for k, v in sorted(counts.items())))
    gate_counts = payload.get("gate_counts") or {}
    if gate_counts:
        L.append("- 核验门发布决定：" + "、".join(f"{k} {v}" for k, v in sorted(gate_counts.items())))
    L.append("")

    L.append("## 二、逐条结论")
    L.append("")
    if not findings:
        L.append("（本次没有产出具名结论。）")
        L.append("")
    for f in findings:
        L.append(f"### {f.get('finding_id', '')}　{f.get('verdict', '')}"
                 f"　（主张 {f.get('claim_id', '')}）")
        if f.get("error_type"):
            L.append(f"- 错误类型：{f['error_type']}")
        if f.get("basis"):
            L.append(f"- 依据：{f['basis']}")
        if f.get("suggested_fix"):
            L.append(f"- 修正建议：{f['suggested_fix']}")
        pages = f.get("citation_pages") or []
        if pages:
            L.append("- 引用页：" + "、".join(str(p) for p in pages))
        for ev in (f.get("evidence") or []):
            if isinstance(ev, dict) and ev.get("file_name"):
                L.append(f"  - {ev.get('file_name')} 第 {ev.get('page')} 页"
                         f"　原值 {ev.get('raw', ev.get('value', ''))}")
        if f.get("assertion_refs"):
            L.append("- 引用断言：" + "、".join(map(str, f["assertion_refs"])))
        if f.get("report_labels"):
            L.append("- 对外标签：" + "、".join(f["report_labels"]))
        L.append(f"- 发布决定：**{f.get('publish', '')}**")
        reasons = f.get("block_reasons") or []
        if reasons:
            for r in reasons:
                L.append(f"  - {r}")
        L.append("")

    L.append("## 三、未解决清单（必须随报告输出）")
    L.append("")
    if not residuals:
        L.append("（无。）")
    for r in residuals:
        L.append(f"- [{r.get('kind', '')}] {r.get('text', '')}"
                 + (f"　主张 {r.get('claim_id')}" if r.get("claim_id") else "")
                 + (f"　原因：{r.get('reason')}" if r.get("reason") else ""))
    L.append("")

    L.append("## 四、证据分层")
    L.append("")
    L.append("- 一手证据：来自官方披露原件，逐条带页码与原文，可回查。")
    L.append("- 二手线索：网页搜索等线索层内容，只用于指引调查方向，**不单独支撑任何结论**。")
    L.append("")

    L.append("---")
    L.append("")
    L.append(DISCLAIMER)
    L.append("")

    text = "\n".join(L)
    guard_text(text)                                  # 措辞守门人：不过就抛，不落半成品
    return text


def write_outputs(run_dir: Path, payload: dict) -> dict:
    """落 `findings.json` 与 `report.md`，返回两个路径。"""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    md = render_markdown(payload)
    fj = run_dir / "findings.json"
    rm = run_dir / "report.md"
    fj.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    rm.write_text(md, encoding="utf-8")
    return {"findings_json": str(fj), "report_md": str(rm)}
