"""步骤 ⑥⑦：异常评分 → 风险假设 → 核查建议。

只做「信号」不做「结论」。每条假设都带：触发规则、复算证据、建议核查动作。
严重度只用于排序，不代表对主体的定性。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..models import RecomputeResult, RawValue


@dataclass
class Hypothesis:
    code: str
    title: str                 # 风险信号名称（中性描述）
    severity: str              # high | medium | low —— 只用于排序
    basis: str                 # 触发依据（含复算数字）
    suggested_checks: list[str] = field(default_factory=list)
    label: str = "RISK_SIGNAL_DETECTED"
    metrics: list[str] = field(default_factory=list)


@dataclass
class Thresholds:
    cash_ratio_low: float = 0.70          # 收现比低于此值 → 信号
    ar_growth_gap_high: float = 0.20      # 应收增速 - 收入增速 高于此值 → 信号
    gross_margin_swing: float = 0.05      # 毛利率同比变动超过 5pp → 信号


def score(
    *,
    cash_ratio: RecomputeResult | None,
    ar_gap: RecomputeResult | None,
    gm_cur: RecomputeResult | None,
    gm_prev: RecomputeResult | None,
    thresholds: Thresholds | None = None,
) -> list[Hypothesis]:
    t = thresholds or Thresholds()
    out: list[Hypothesis] = []

    if cash_ratio and cash_ratio.result is not None:
        if cash_ratio.result < t.cash_ratio_low:
            out.append(
                Hypothesis(
                    code="CASH_CONVERSION_WEAK",
                    title="收入现金含量偏低",
                    severity="high" if cash_ratio.result < 0.5 else "medium",
                    basis=(
                        f"复算收现比 = {cash_ratio.result:.3f}，低于阈值 {t.cash_ratio_low}。"
                        f"公式：{cash_ratio.formula}。"
                    ),
                    suggested_checks=[
                        "核对现金流量表「销售商品、提供劳务收到的现金」与利润表营业收入的列报口径是否一致",
                        "查阅应收账款、应收票据、合同资产的附注，确认是否存在大额未收回款项",
                        "核查是否存在以票据、保理等方式提前确认收入的情形",
                    ],
                    metrics=["销售商品、提供劳务收到的现金", "营业收入"],
                )
            )
    elif cash_ratio and cash_ratio.error:
        out.append(
            Hypothesis(
                code="CASH_CONVERSION_UNKNOWN",
                title="收入现金含量无法复算",
                severity="low",
                basis=f"复算未完成：{cash_ratio.error}",
                suggested_checks=["补齐所需科目后重跑复算"],
                label="INSUFFICIENT_INFORMATION",
            )
        )

    if ar_gap and ar_gap.result is not None:
        if ar_gap.result > t.ar_growth_gap_high:
            out.append(
                Hypothesis(
                    code="RECEIVABLE_GROWTH_FASTER",
                    title="应收账款增速显著快于营业收入增速",
                    severity="high" if ar_gap.result > 0.5 else "medium",
                    basis=(
                        f"复算增速差 = {ar_gap.result:+.1%}，高于阈值 {t.ar_growth_gap_high:.0%}。"
                        f"公式：{ar_gap.formula}。"
                    ),
                    suggested_checks=[
                        "核对应收账款账龄结构与前五大欠款方，关注是否集中在新增客户",
                        "对比信用政策是否发生变化（账期、折扣、结算方式）",
                        "查阅报告期后回款情况以及坏账准备计提比例是否同步调整",
                    ],
                    metrics=["应收账款", "营业收入"],
                )
            )

    if gm_cur and gm_prev and gm_cur.result is not None and gm_prev.result is not None:
        swing = gm_cur.result - gm_prev.result
        if abs(swing) > t.gross_margin_swing:
            out.append(
                Hypothesis(
                    code="GROSS_MARGIN_SWING",
                    title="毛利率同比出现较大波动",
                    severity="medium",
                    basis=(
                        f"毛利率由 {gm_prev.result:.2%} 变为 {gm_cur.result:.2%}，"
                        f"变动 {swing:+.2%}，超过阈值 ±{t.gross_margin_swing:.0%}。"
                    ),
                    suggested_checks=[
                        "核对营业成本构成明细，区分原材料价格、产品结构与制造费用口径变化",
                        "关注收入确认时点是否变化（总额法/净额法切换会导致毛利率跳变）",
                        "与同行业可比公司同期毛利率变动方向对比",
                    ],
                    metrics=["营业收入", "营业成本"],
                )
            )

    order = {"high": 0, "medium": 1, "low": 2}
    out.sort(key=lambda h: order.get(h.severity, 9))
    return out


def has_authoritative_confirmation(values: list[RawValue]) -> bool:
    """判断输入里是否存在监管/公司正式确认文件。

    mock 阶段用显式标记位：任意 RawValue 的 metric 为
    `__authoritative_confirmation__` 且 value > 0 即视为存在。
    接真实数据后应改为检查是否存在 source_type == 'inquiry_letter_reply' 且
    正文含认定结论的文档。
    """
    return any(v.metric == "__authoritative_confirmation__" and v.value > 0 for v in values)
