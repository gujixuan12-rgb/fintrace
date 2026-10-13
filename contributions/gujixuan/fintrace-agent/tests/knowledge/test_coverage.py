"""覆盖性：resolve_metric 对引擎 METRICS 的每一个 cn / 别名 / 指标键别名都必须命中。"""
from __future__ import annotations

from fintrace_engine import draft_review as dr


def test_all_metric_names_resolve(kb):
    total = 0
    for metric_id, spec in dr.METRICS.items():
        for name in [spec["cn"], *spec["aliases"]]:
            got = kb.resolve_metric(name)
            assert got is not None, f"未命中：{name!r}"
            assert got["metric_id"] == metric_id, (name, got["metric_id"], metric_id)
            assert got["base_unit"] == spec["unit"]
            assert "caliber" in got and "domain" in got
            total += 1

    for alias, canonical in dr.METRIC_KEY_ALIASES.items():
        got = kb.resolve_metric(alias)
        assert got is not None, f"指标键别名未命中：{alias!r}"
        assert got["metric_id"] == canonical, (alias, got["metric_id"], canonical)
        total += 1

    expected = sum(1 + len(s["aliases"]) for s in dr.METRICS.values()) + len(dr.METRIC_KEY_ALIASES)
    print(f"全部指标命中 {total} 个")
    assert total == expected


def test_resolve_nearest_to_anchor(kb):
    """锚点取离数值最近的名字，而不是最长的那个（引擎同规则）。"""
    text = "公司对应 2023 年归母净利润的市盈率约为 41 倍"
    anchor = text.index("41")
    got = kb.resolve_metric(text, anchor=anchor)
    assert got is not None
    assert got["metric_id"] == "pe_ratio"


def test_resolve_miss_returns_none(kb):
    assert kb.resolve_metric("这句话里没有任何已知指标") is None
    assert kb.resolve_metric("") is None
