from __future__ import annotations

from decimal import Decimal
from typing import Any

from .normalizers import NormalizationError, normalize_amount, to_decimal


AMBIGUOUS_METRICS = {
    "net_profit": "净利润口径不明确，必须说明归母净利润、扣非归母净利润或合并净利润。",
}


def _issue(claim_id: str, code: str, message: str, severity: str = "ERROR") -> dict[str, str]:
    return {"claim_id": claim_id, "code": code, "severity": severity, "message": message}


def _ledger_index(ledger: dict[str, Any]) -> dict[tuple[str, str, str], dict[str, Any]]:
    return {
        (item["metric_id"], str(item["period"]), item["scope"]): item
        for item in ledger.get("entries", [])
    }


def _reference_for(claim: dict[str, Any], index: dict[tuple[str, str, str], dict[str, Any]]) -> dict[str, Any] | None:
    metric_id = claim.get("metric_id", "")
    period = str(claim.get("period", ""))
    scope = claim.get("scope")
    if scope:
        return index.get((metric_id, period, scope))
    matches = [entry for (m, p, _), entry in index.items() if m == metric_id and p == period]
    return matches[0] if len(matches) == 1 else None


def _numeric_matches(claim: dict[str, Any], reference: dict[str, Any]) -> tuple[bool, Decimal, Decimal]:
    actual = normalize_amount(claim["value"], claim["unit"], "元")
    expected = normalize_amount(reference["value"], reference["unit"], "元")
    if claim.get("approximate"):
        tolerance = abs(expected) * to_decimal(claim.get("relative_tolerance", "0.01"))
    else:
        tolerance = to_decimal(claim.get("absolute_tolerance_yuan", "0.01"))
    return abs(actual - expected) <= tolerance, actual, expected


def check_claims(claims_payload: dict[str, Any], ledger: dict[str, Any], features: dict[str, str]) -> dict[str, Any]:
    index = _ledger_index(ledger)
    issues: list[dict[str, str]] = []
    results: list[dict[str, Any]] = []

    for claim in claims_payload.get("claims", []):
        claim_id = claim["claim_id"]
        claim_issues: list[dict[str, str]] = []
        metric_id = claim.get("metric_id", "")
        claim_type = claim.get("claim_type", "amount")

        if metric_id in AMBIGUOUS_METRICS:
            claim_issues.append(_issue(claim_id, "METRIC_AMBIGUOUS", AMBIGUOUS_METRICS[metric_id]))
        elif claim_type == "ratio":
            feature_key = claim.get("feature_key", "")
            if feature_key not in features:
                claim_issues.append(_issue(claim_id, "REFERENCE_MISSING", f"未找到计算特征: {feature_key}"))
            else:
                expected = to_decimal(features[feature_key])
                reported = to_decimal(claim["value"])
                if claim.get("unit") == "%":
                    reported /= Decimal("100")
                tolerance = to_decimal(claim.get("absolute_tolerance", "0.001"))
                if abs(reported - expected) > tolerance:
                    claim_issues.append(_issue(claim_id, "VALUE_CONFLICT", f"报告值 {reported} 与确定性计算值 {expected} 不一致"))
        else:
            reference = _reference_for(claim, index)
            if not reference:
                if not claim.get("scope"):
                    claim_issues.append(_issue(claim_id, "SCOPE_REQUIRED", "缺少统计口径，无法选择唯一参考值"))
                else:
                    claim_issues.append(_issue(claim_id, "REFERENCE_MISSING", "未找到同指标、期间和口径的参考值"))
            else:
                cited_page = claim.get("cited_page")
                if cited_page is not None and cited_page not in reference.get("evidence_pages", []):
                    expected_pages = ", ".join(map(str, reference.get("evidence_pages", [])))
                    claim_issues.append(_issue(claim_id, "CITATION_MISMATCH", f"引用第{cited_page}页，参考证据页为{expected_pages}"))

                if claim_type == "amount" and "value" in claim:
                    try:
                        matches, actual, expected = _numeric_matches(claim, reference)
                        if not matches:
                            claim_issues.append(_issue(claim_id, "VALUE_CONFLICT", f"统一为元后，报告值 {actual} 与参考值 {expected} 不一致"))
                    except NormalizationError as exc:
                        claim_issues.append(_issue(claim_id, "UNIT_ERROR", str(exc)))

                if claim_type == "direction":
                    prior_key = (metric_id, str(claim.get("prior_period")), reference["scope"])
                    prior = index.get(prior_key)
                    if not prior:
                        claim_issues.append(_issue(claim_id, "REFERENCE_MISSING", "缺少用于判断方向的上期数据"))
                    else:
                        current_value = normalize_amount(reference["value"], reference["unit"], "元")
                        prior_value = normalize_amount(prior["value"], prior["unit"], "元")
                        expected_direction = "increase" if current_value > prior_value else "decrease" if current_value < prior_value else "flat"
                        if claim.get("direction") != expected_direction:
                            claim_issues.append(_issue(claim_id, "DIRECTION_CONFLICT", f"报告方向为 {claim.get('direction')}，参考数据方向为 {expected_direction}"))

        status = "PASS" if not claim_issues else "REVIEW_REQUIRED"
        results.append({"claim_id": claim_id, "status": status, "issues": claim_issues})
        issues.extend(claim_issues)

    return {
        "document_name": claims_payload.get("document_name"),
        "checked_claims": len(results),
        "passed_claims": sum(item["status"] == "PASS" for item in results),
        "review_required_claims": sum(item["status"] != "PASS" for item in results),
        "results": results,
        "issues": issues,
    }
