from __future__ import annotations

from html import escape
from pathlib import Path
from typing import Any


def _list(items: list[str]) -> str:
    return "".join(f"<li>{escape(str(item))}</li>" for item in items)


def render_html(result: dict[str, Any], output_path: str | Path) -> None:
    analysis = result["analysis"]
    explanation = result["agent_explanation"]
    fact = result.get("fact_check") or {"issues": [], "checked_claims": 0, "passed_claims": 0}
    signals = "".join(
        "<tr>"
        f"<td>{escape(item['signal_id'])}</td>"
        f"<td>{escape(item['signal'])}</td>"
        f"<td>{escape(item['hypothesis'])}</td>"
        "</tr>"
        for item in analysis.get("risk_signals", [])
    )
    issues = "".join(
        "<tr>"
        f"<td>{escape(item['claim_id'])}</td>"
        f"<td>{escape(item['code'])}</td>"
        f"<td>{escape(item['message'])}</td>"
        "</tr>"
        for item in fact.get("issues", [])
    ) or '<tr><td colspan="3">未发现需要复核的结构化事实声明</td></tr>'
    evaluation = result.get("evaluation")
    threshold = analysis.get("peer_threshold_assessment", {})
    threshold_status = threshold.get("status", "NOT_PROVIDED")
    threshold_sample = threshold.get("sample_size")
    threshold_value = threshold_status if threshold_sample is None else f"{threshold_status}（n={threshold_sample}）"
    evaluation_html = ""
    if evaluation:
        evaluation_html = (
            "<section><h2>事后评测</h2>"
            f"<p>方向覆盖：{evaluation['matched_label_dimensions']}/{evaluation['label_dimension_count']}</p>"
            f"<p class='limit'>{escape(evaluation['interpretation_limit'])}</p></section>"
        )

    html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FinTrace 风险预警报告</title>
<style>
body{{font-family:Arial,"Microsoft YaHei",sans-serif;margin:0;background:#f5f7fa;color:#1f2937}}
main{{max-width:1100px;margin:32px auto;padding:0 24px 48px}}h1{{color:#17365d;margin-bottom:6px}}h2{{color:#17365d;margin-top:0}}
.sub,.limit{{color:#5b6573}}.cards{{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin:24px 0}}
.card,section{{background:white;border:1px solid #dbe3ec;border-radius:10px;padding:20px;margin-bottom:18px}}.value{{font-size:30px;font-weight:700;color:#17365d}}
table{{border-collapse:collapse;width:100%}}th{{background:#17365d;color:white;text-align:left}}th,td{{padding:12px;border:1px solid #dbe3ec;vertical-align:top}}
.warn{{color:#9c0006;font-weight:700}}ul{{line-height:1.75}}@media(max-width:760px){{.cards{{grid-template-columns:1fr}}}}
</style></head><body><main>
<h1>FinTrace 事前财务风险预警</h1><p class="sub">{escape(analysis['company'])}（{escape(analysis['stock_code'])}） 截止日 {escape(analysis['as_of_date'])}</p>
<div class="cards"><div class="card"><div>结构性信号</div><div class="value">{len(analysis.get('risk_signals', []))}</div></div>
<div class="card"><div>事实声明检查</div><div class="value">{fact.get('checked_claims', 0)}</div></div>
<div class="card"><div>需要复核</div><div class="value warn">{fact.get('review_required_claims', 0)}</div></div></div>
<section><h2>结论</h2><p>{escape(explanation['headline'])}</p><p class="limit">{escape(explanation['conclusion_boundary'])}</p></section>
<section><h2>风险信号</h2><table><thead><tr><th>编号</th><th>信号</th><th>解释</th></tr></thead><tbody>{signals}</tbody></table></section>
<section><h2>同行阈值状态</h2><p><strong>{escape(threshold_value)}</strong></p><p class="limit">{escape(threshold.get('message', '尚未提供同行样本。'))}</p></section>
<section><h2>材料事实核验</h2><table><thead><tr><th>声明</th><th>问题代码</th><th>修改意见</th></tr></thead><tbody>{issues}</tbody></table></section>
<section><h2>建议核查程序</h2><ul>{_list(analysis.get('recommended_procedures', []))}</ul></section>
<section><h2>证据缺口与限制</h2><ul>{_list(analysis.get('evidence_gaps', []))}</ul></section>
{evaluation_html}
</main></body></html>"""
    Path(output_path).write_text(html, encoding="utf-8")
