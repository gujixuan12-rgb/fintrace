from __future__ import annotations

from decimal import Decimal
from typing import Any

from .normalizers import normalize_amount, require_same_scope


class CalculationError(ValueError):
    """Raised when a requested calculation is not valid."""


def safe_divide(numerator: Decimal, denominator: Decimal) -> Decimal:
    if denominator == 0:
        raise CalculationError("分母为0")
    return numerator / denominator


def growth_rate(current: Decimal, prior: Decimal) -> Decimal:
    return safe_divide(current - prior, prior)


def _point(financials: dict[str, Any], year: str, metric: str) -> dict[str, Any]:
    try:
        return financials[year][metric]
    except KeyError as exc:
        raise CalculationError(f"缺少输入: {year}.{metric}") from exc


def _amount(point: dict[str, Any]) -> Decimal:
    return normalize_amount(point.get("value"), point.get("unit", ""), "元")


def calculate_features(financials: dict[str, Any]) -> dict[str, Decimal]:
    rev19_p = _point(financials, "2019", "revenue")
    rev20_p = _point(financials, "2020", "revenue")
    np19_p = _point(financials, "2019", "attributable_net_profit")
    np20_p = _point(financials, "2020", "attributable_net_profit")
    ocf19_p = _point(financials, "2019", "operating_cash_flow")
    ocf20_p = _point(financials, "2020", "operating_cash_flow")
    ta20_p = _point(financials, "2020", "total_assets")
    ar20_p = _point(financials, "2020", "accounts_receivable")
    ca20_p = _point(financials, "2020", "contract_assets")

    require_same_scope(rev19_p["scope"], rev20_p["scope"], ocf19_p["scope"], ocf20_p["scope"])
    require_same_scope(np19_p["scope"], np20_p["scope"])
    require_same_scope(ta20_p["scope"], ar20_p["scope"], ca20_p["scope"])

    rev19, rev20 = _amount(rev19_p), _amount(rev20_p)
    np19, np20 = _amount(np19_p), _amount(np20_p)
    ocf19, ocf20 = _amount(ocf19_p), _amount(ocf20_p)
    ta20, ar20, ca20 = _amount(ta20_p), _amount(ar20_p), _amount(ca20_p)

    revenue_growth = growth_rate(rev20, rev19)
    profit_growth = growth_rate(np20, np19)
    return {
        "ocf_to_revenue_2019": safe_divide(ocf19, rev19),
        "ocf_to_revenue_2020": safe_divide(ocf20, rev20),
        "ocf_to_attributable_profit_2019": safe_divide(ocf19, np19),
        "ocf_to_attributable_profit_2020": safe_divide(ocf20, np20),
        "revenue_growth_2020": revenue_growth,
        "attributable_profit_growth_2020": profit_growth,
        "revenue_profit_growth_divergence_2020": revenue_growth - profit_growth,
        "receivables_contract_assets_to_total_assets_2020": safe_divide(ar20 + ca20, ta20),
    }


def decimal_strings(features: dict[str, Decimal]) -> dict[str, str]:
    return {key: format(value, "f") for key, value in features.items()}
