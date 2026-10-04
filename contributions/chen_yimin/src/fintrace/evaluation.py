from __future__ import annotations

from typing import Any


def evaluate_case(risk_output: dict[str, Any], evaluation_map: dict[str, Any]) -> dict[str, Any]:
    triggered = {item["signal_id"] for item in risk_output.get("risk_signals", [])}
    rows: list[dict[str, Any]] = []
    for label in evaluation_map.get("label_dimensions", []):
        supporting = set(label.get("supporting_signal_ids", []))
        matched = sorted(triggered & supporting)
        rows.append({
            "label_id": label["label_id"],
            "label": label["label"],
            "matched": bool(matched),
            "matched_signal_ids": matched,
        })

    matched_count = sum(row["matched"] for row in rows)
    total = len(rows)
    return {
        "case_count": 1,
        "label_dimension_count": total,
        "matched_label_dimensions": matched_count,
        "descriptive_coverage": None if total == 0 else format(matched_count / total, ".6f"),
        "details": rows,
        "interpretation_limit": "该结果仅描述一个开发案例中的方向覆盖，不是准确率、召回率或样本外预测能力。",
        "leakage_policy": "事后标签仅在风险信号生成后加载。",
    }
