from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from statistics import median
from typing import Iterable


MAD_SCALE = Decimal("1.4826")


@dataclass(frozen=True)
class ThresholdAssessment:
    status: str
    sample_size: int
    median: Decimal | None
    mad: Decimal | None
    robust_z: Decimal | None
    percentile_rank: Decimal | None
    message: str


def _as_decimals(values: Iterable[Decimal | int | float | str]) -> list[Decimal]:
    result = []
    for value in values:
        if isinstance(value, bool):
            raise ValueError('布尔值不能作为财务指标')
        number = value if isinstance(value, Decimal) else Decimal(str(value))
        if not number.is_finite():
            raise ValueError('指标必须是有限数值')
        result.append(number)
    return result


def assess_peer_value(
    target: Decimal | int | float | str,
    peer_values: Iterable[Decimal | int | float | str],
    *,
    min_sample_size: int = 10,
) -> ThresholdAssessment:
    """Assess one value against same-year, same-definition peers.

    The caller must perform time, scope and source-quality filtering before
    passing peer values. Small samples are reported as unavailable rather than
    converted into apparently precise P10/P90 thresholds.
    """
    if isinstance(min_sample_size, bool) or not isinstance(min_sample_size, int) or min_sample_size < 2:
        raise ValueError('最低样本数必须是至少2的整数')
    peers = sorted(_as_decimals(peer_values))
    target_value = _as_decimals([target])[0]
    if len(peers) < min_sample_size:
        return ThresholdAssessment(
            status="INSUFFICIENT_PEER_SAMPLE",
            sample_size=len(peers),
            median=None,
            mad=None,
            robust_z=None,
            percentile_rank=None,
            message=f"同行有效样本仅 {len(peers)} 个，低于最低要求 {min_sample_size} 个，不输出行业阈值。",
        )

    center = median(peers)
    deviations = [abs(value - center) for value in peers]
    mad = median(deviations)
    robust_z = None if mad == 0 else abs(target_value - center) / (MAD_SCALE * mad)
    less_or_equal = sum(value <= target_value for value in peers)
    percentile_rank = Decimal(less_or_equal) / Decimal(len(peers))
    return ThresholdAssessment(
        status="AVAILABLE",
        sample_size=len(peers),
        median=center,
        mad=mad,
        robust_z=robust_z,
        percentile_rank=percentile_rank,
        message="仅作为统计异常线索，仍需结合原始披露和业务背景核验。",
    )
