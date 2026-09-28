"""步骤 ④：确定性计算（Python 复算，不让模型口算）。

所有公式集中在这里，只做纯函数计算，不碰 IO、不调模型。
每个公式都要求输入齐备、单位一致，缺输入就返回 error，绝不猜。
"""
from __future__ import annotations

from dataclasses import dataclass

from ..models import RawValue, RecomputeResult


class UnitError(ValueError):
    pass


#: 单位换算到「元」的倍率。金额类指标统一在这里收敛。
UNIT_SCALE = {
    "元": 1.0,
    "万元": 1e4,
    "亿元": 1e8,
    "千元": 1e3,
}


def to_yuan(v: RawValue) -> float:
    if v.unit not in UNIT_SCALE:
        raise UnitError(f"未知金额单位 {v.unit!r}（指标 {v.metric}）")
    return v.value * UNIT_SCALE[v.unit]


def _index(values: list[RawValue], period: str) -> dict[str, RawValue]:
    return {v.metric: v for v in values if v.period == period}


def gross_margin(values: list[RawValue], period: str) -> RecomputeResult:
    """毛利率 = 1 - 营业成本 / 营业收入"""
    idx = _index(values, period)
    need = ("营业收入", "营业成本")
    missing = [m for m in need if m not in idx]
    if missing:
        return RecomputeResult(
            formula="毛利率 = 1 - 营业成本 / 营业收入",
            inputs={},
            error=f"缺少输入：{', '.join(missing)}",
        )
    rev = to_yuan(idx["营业收入"])
    cost = to_yuan(idx["营业成本"])
    if rev == 0:
        return RecomputeResult(formula="毛利率 = 1 - 营业成本 / 营业收入",
                               inputs={"营业收入": rev, "营业成本": cost},
                               error="营业收入为 0，无法计算")
    v = 1 - cost / rev
    return RecomputeResult(
        formula="毛利率 = 1 - 营业成本 / 营业收入",
        inputs={"营业收入": rev, "营业成本": cost},
        result=round(v, 6),
        unit="ratio",
    )


def operating_cash_to_revenue(values: list[RawValue], period: str) -> RecomputeResult:
    """收现比 = 销售商品、提供劳务收到的现金 / 营业收入"""
    idx = _index(values, period)
    a, b = "销售商品、提供劳务收到的现金", "营业收入"
    if a not in idx or b not in idx:
        return RecomputeResult(
            formula=f"收现比 = {a} / {b}",
            inputs={},
            error=f"缺少输入：{', '.join(m for m in (a, b) if m not in idx)}",
        )
    num, den = to_yuan(idx[a]), to_yuan(idx[b])
    if den == 0:
        return RecomputeResult(formula=f"收现比 = {a} / {b}",
                               inputs={a: num, b: den}, error="营业收入为 0")
    return RecomputeResult(
        formula=f"收现比 = {a} / {b}",
        inputs={a: num, b: den},
        result=round(num / den, 6),
        unit="ratio",
    )


def receivable_growth_gap(values: list[RawValue], prev: str, cur: str) -> RecomputeResult:
    """应收账款增速 - 营业收入增速（差值越大越可疑）"""
    p, c = _index(values, prev), _index(values, cur)
    need = ("应收账款", "营业收入")
    missing = [m for m in need if m not in p or m not in c]
    if missing:
        return RecomputeResult(
            formula=f"应收账款增速({cur}) - 营业收入增速({cur})",
            inputs={},
            error=f"缺少 {prev} 或 {cur} 的输入：{', '.join(missing)}",
        )
    def growth(idx, m):
        a, b = idx[m].value, p[m].value
        if b == 0:
            raise UnitError(f"{m} 在 {prev} 为 0")
        return a / b - 1
    try:
        g_ar, g_rev = growth(c, "应收账款"), growth(c, "营业收入")
    except UnitError as e:
        return RecomputeResult(formula="应收增速差", inputs={}, error=str(e))
    return RecomputeResult(
        formula=f"应收账款增速({cur}) - 营业收入增速({cur})",
        inputs={"应收账款": c["应收账款"].value, "营业收入": c["营业收入"].value},
        result=round(g_ar - g_rev, 6),
        unit="ratio",
    )


#: 供 orchestrator 按名称调用的注册表。
REGISTRY = {
    "gross_margin": gross_margin,
    "operating_cash_to_revenue": operating_cash_to_revenue,
    "receivable_growth_gap": receivable_growth_gap,
}
