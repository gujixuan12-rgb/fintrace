from __future__ import annotations

from typing import Any

from .agent import deterministic_explanation, openai_explanation
from .evaluation import evaluate_case
from .fact_checker import check_claims
from .pipeline import run_case


def run_fintrace(
    case: dict[str, Any],
    *,
    claims: dict[str, Any] | None = None,
    ledger: dict[str, Any] | None = None,
    evaluation_map: dict[str, Any] | None = None,
    use_llm: bool = False,
) -> dict[str, Any]:
    analysis = run_case(case)
    fact_check = None
    if claims is not None and ledger is not None:
        fact_check = check_claims(claims, ledger, analysis["features"])

    explanation = openai_explanation(analysis, fact_check) if use_llm else deterministic_explanation(analysis, fact_check)
    result: dict[str, Any] = {
        "pipeline_version": "1.1.0",
        "stages": ["source_guard", "fact_check", "deterministic_calculation", "risk_explanation"],
        "fact_check": fact_check,
        "analysis": analysis,
        "agent_explanation": explanation,
    }
    if evaluation_map is not None:
        result["evaluation"] = evaluate_case(analysis, evaluation_map)
        result["stages"].append("ex_post_evaluation")
    return result
