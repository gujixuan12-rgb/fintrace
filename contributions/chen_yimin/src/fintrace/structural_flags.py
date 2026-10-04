from __future__ import annotations

from decimal import Decimal


def build_structural_flags(features: dict[str, Decimal]) -> list[dict[str, str]]:
    flags: list[dict[str, str]] = []
    if features["ocf_to_revenue_2019"] > 0 and features["ocf_to_revenue_2020"] < 0:
        flags.append({
            "signal_id": "CASH_001",
            "signal": "经营现金流相对收入由正转负",
            "hypothesis": "现金回收与收入确认节奏可能出现背离，需要进一步核查回款和项目结算。",
        })
    if features["revenue_growth_2020"] > 0 and features["attributable_profit_growth_2020"] < 0:
        flags.append({
            "signal_id": "DIV_001",
            "signal": "营业收入增长但归母净利润下降",
            "hypothesis": "收入增长未转化为归母利润增长，需要拆解毛利、费用、减值和非经常性项目。",
        })
    if features["ocf_to_revenue_2020"] < 0 and features["ocf_to_attributable_profit_2020"] < 0:
        flags.append({
            "signal_id": "QUALITY_001",
            "signal": "经营现金流为负但归母净利润为正",
            "hypothesis": "利润的现金支撑较弱，需要核查应收账款、合同资产及经营性往来。",
        })
    return flags
