from __future__ import annotations

from typing import Any

from .calculations import calculate_features, decimal_strings
from .source_guard import validate_sources
from .structural_flags import build_structural_flags
from .thresholds import assess_peer_value
from .peer_guard import filter_peers


def run_case(case: dict[str, Any]) -> dict[str, Any]:
    warnings = validate_sources(case)
    raw_features = calculate_features(case["financials"])
    flags = build_structural_flags(raw_features)
    peer_benchmark = case.get("peer_benchmark")
    if peer_benchmark:
        metric = peer_benchmark["metric"]
        peer_values, accepted, excluded = filter_peers(peer_benchmark, case)
        assessment = assess_peer_value(
            raw_features[metric],
            peer_values,
            min_sample_size=peer_benchmark.get("min_sample_size", 10),
        )
        threshold_result = {
            "metric": metric,
            "accepted_records": accepted,
            "excluded_records": excluded,
            "threshold_policy": "最低样本量为项目可配置规则，不是统计可靠性的保证。",
            "status": assessment.status,
            "sample_size": assessment.sample_size,
            "median": None if assessment.median is None else str(assessment.median),
            "mad": None if assessment.mad is None else str(assessment.mad),
            "robust_z": None if assessment.robust_z is None else str(assessment.robust_z),
            "percentile_rank": None if assessment.percentile_rank is None else str(assessment.percentile_rank),
            "message": assessment.message,
        }
    else:
        threshold_result = {
            "status": "NOT_PROVIDED",
            "message": "尚未提供通过时间、口径和来源质量筛选的同行样本。",
        }
    return {
        "case_id": case["case_id"],
        "company": case["company"],
        "stock_code": case["stock_code"],
        "as_of_date": case["as_of_date"],
        "model_role": "事前风险预警，不构成对错报、舞弊或违法的认定",
        "features": decimal_strings(raw_features),
        "risk_signals": flags,
        "peer_threshold_assessment": threshold_result,
        "evidence_gaps": warnings + [
            "单一案例不能支持行业阈值，当前仅使用方向和符号关系规则。",
            "需要补充同行业、同口径历史样本后再校准连续指标阈值。",
        ],
        "recommended_procedures": [
            "核对重大工程项目合同、进度依据、结算节点与收入确认时点。",
            "对大额应收账款和合同资产执行函证、期后回款检查及账龄分析。",
            "复核合同资产减值模型、合同变更和预计总成本更新记录。",
            "将经营现金流变化与客户、供应商及银行流水明细交叉核对。",
        ],
    }
