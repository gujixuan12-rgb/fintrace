from __future__ import annotations

from decimal import Decimal, InvalidOperation


UNIT_FACTORS = {
    "元": Decimal("1"),
    "万元": Decimal("10000"),
    "亿元": Decimal("100000000"),
}


class NormalizationError(ValueError):
    """Raised when a financial input cannot be normalized safely."""


def to_decimal(value: object) -> Decimal:
    if value is None or isinstance(value, bool):
        raise NormalizationError("数值缺失或类型无效")
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, AttributeError) as exc:
        raise NormalizationError(f"无法解析数值: {value!r}") from exc


def normalize_amount(value: object, unit: str, target_unit: str = "元") -> Decimal:
    if unit not in UNIT_FACTORS or target_unit not in UNIT_FACTORS:
        raise NormalizationError(f"不支持的金额单位: {unit} -> {target_unit}")
    amount = to_decimal(value)
    return amount * UNIT_FACTORS[unit] / UNIT_FACTORS[target_unit]


def require_same_scope(*scopes: str) -> None:
    cleaned = {scope.strip() for scope in scopes if scope and scope.strip()}
    if len(cleaned) > 1:
        raise NormalizationError(f"口径不一致: {sorted(cleaned)}")
