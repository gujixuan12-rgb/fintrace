from __future__ import annotations

import json
import os
from typing import Any


def deterministic_explanation(analysis: dict[str, Any], fact_check: dict[str, Any] | None = None) -> dict[str, Any]:
    signals = analysis.get("risk_signals", [])
    issues = [] if fact_check is None else fact_check.get("issues", [])
    return {
        "headline": f"发现 {len(signals)} 项结构性风险信号，{len(issues)} 项材料事实需要复核。",
        "risk_explanations": [
            {
                "signal_id": item["signal_id"],
                "signal": item["signal"],
                "interpretation": item["hypothesis"],
            }
            for item in signals
        ],
        "fact_check_actions": [item["message"] for item in issues],
        "recommended_procedures": analysis.get("recommended_procedures", []),
        "limitations": analysis.get("evidence_gaps", []),
        "conclusion_boundary": "输出用于确定进一步核查方向，不构成对错报、舞弊或违法的认定。",
        "generation_mode": "deterministic_template",
    }


def openai_explanation(analysis: dict[str, Any], fact_check: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("未安装 openai 包；核心确定性流程仍可正常运行") from exc

    model = os.environ.get("FINTRACE_MODEL", "gpt-6-astra")
    schema = {
        "type": "object",
        "properties": {
            "headline": {"type": "string"},
            "risk_explanations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "signal_id": {"type": "string"},
                        "signal": {"type": "string"},
                        "interpretation": {"type": "string"}
                    },
                    "required": ["signal_id", "signal", "interpretation"],
                    "additionalProperties": False
                }
            },
            "fact_check_actions": {"type": "array", "items": {"type": "string"}},
            "recommended_procedures": {"type": "array", "items": {"type": "string"}},
            "limitations": {"type": "array", "items": {"type": "string"}},
            "conclusion_boundary": {"type": "string"}
        },
        "required": ["headline", "risk_explanations", "fact_check_actions", "recommended_procedures", "limitations", "conclusion_boundary"],
        "additionalProperties": False
    }
    payload = {"analysis": analysis, "fact_check": fact_check or {}}
    response = OpenAI().responses.create(
        model=model,
        input=[
            {
                "role": "system",
                "content": (
                    "你是金融风险预警解释器。只能解释输入中的确定性结果；不得新增数字、事实或来源；"
                    "不得把风险信号表述为已确认的错报、舞弊或违法；明确列出证据缺口。"
                ),
            },
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        text={"format": {"type": "json_schema", "name": "fintrace_explanation", "strict": True, "schema": schema}},
    )
    result = json.loads(response.output_text)
    result["generation_mode"] = "openai_structured_output"
    return result
