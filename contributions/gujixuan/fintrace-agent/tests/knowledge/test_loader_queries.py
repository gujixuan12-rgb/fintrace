"""loader 的四个查询接口 + 精度/上下文。"""
from __future__ import annotations

from fintrace_engine import draft_review as dr


def test_precision_for(kb):
    p = kb.precision_for("revenue", "万元")
    assert p["base"] == "元"
    assert p["scale"] == dr.MONEY_SCALE["万元"]
    assert p["precision"] == dr.PRECISION["元"]
    assert p["rounding"] == "ROUND_HALF_UP"

    per10 = kb.precision_for("dividend_per_ten", "元/10股")
    assert per10["precision"] == dr.PRECISION["元/10股"] == 3

    assert kb.precision_for("revenue", "斤") is None


def test_formula_lookup_and_caliber(kb):
    f = kb.formula("cash_dividend")
    assert f and f["formula_id"] == "expected_dividend_yuan"
    assert kb.formula("cash_dividend", "扣非") is None
    assert kb.formula("不存在的指标") is None


def test_relation_rule(kb):
    alias, canonical = next(iter(dr.METRIC_KEY_ALIASES.items()))
    rule = kb.relation_rule(f"metric_key_alias:{alias}")
    assert rule["predicate"] == "equals"
    assert rule["subject_metric"] == alias
    assert rule["object_metric"] == canonical
    assert kb.relation_rule("不存在的规则") is None


def test_all_metrics(kb):
    metrics = kb.all_metrics()
    assert len(metrics) == len(dr.METRICS)
    assert all(m["status"] == "authoritative" for m in metrics)
    assert all({"caliber", "base_unit", "domain"} <= set(m) for m in metrics)


def test_context_check(kb):
    good = kb.context_check(period="2023H1", unit="%")
    assert good["ok"] is True
    assert good["checks"]["unit"]["ok"] is True
    assert good["checks"]["period"]["kind"] == "H1"

    bad_unit = kb.context_check(unit="斤")
    assert bad_unit["ok"] is False

    bad_period = kb.context_check(period="去年")
    assert bad_period["ok"] is False

    assert kb.context_check()["ok"] is False      # 什么都没给：不算通过
