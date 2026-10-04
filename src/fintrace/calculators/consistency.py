"""跨页披露勾稽；只做算术，不判断错报原因。"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any


def _decimal(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("布尔值不能作为财务数值")
    try:
        number = Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("财务数值格式无效") from exc
    if not number.is_finite():
        raise ValueError("财务数值必须有限")
    return number


def dividend_reconciliation(
    shares: Any,
    dividend_per_ten_shares_yuan: Any,
    disclosed_dividend_yuan: Any,
    other_page_total_wan_yuan: Any,
) -> dict[str, str | bool]:
    """股本×每10股派息/10，并比较另一页按万元保留两位的总额。"""
    base = _decimal(shares)
    rate = _decimal(dividend_per_ten_shares_yuan)
    disclosed = _decimal(disclosed_dividend_yuan)
    other = _decimal(other_page_total_wan_yuan)
    if base <= 0 or rate < 0 or disclosed < 0 or other < 0:
        raise ValueError("股本须为正，分红金额不得为负")
    expected = (base * rate / Decimal(10)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    other_rounded = (expected / Decimal(10000)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {
        "formula": "股本×每10股派息额÷10",
        "expected_yuan": str(expected),
        "disclosed_yuan": str(disclosed),
        "difference_yuan": str(expected - disclosed),
        "disclosure_matches": disclosed == expected,
        "other_page_total_wan_yuan": str(other),
        "other_page_matches_after_rounding": other == other_rounded,
    }
