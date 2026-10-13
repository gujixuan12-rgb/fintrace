"""独立语义绑定检查器 B1~B4 与权限不对称（模块 E）。

钉住三个不变量里的两条：
- 两轴分离：只过 W 的断言 evidence_status=attested，semantic_status 仍 unbound；bind 不改 evidence_status。
- 权限不对称：``llm_verdict`` 只能把 bound 拉低成 conflicted，不能把 unbound / conflicted 抬成 bound。
"""
from __future__ import annotations

from fintrace_agent.contracts import Assertion, EvidenceStatus, SemanticStatus, Span
from fintrace_agent.tools.semantic_binding import bind


# ---------------------------------------------------------------------------
# 构造助手
# ---------------------------------------------------------------------------


def _span(**kw) -> Span:
    base = dict(span_id="S-1", doc_id="D-1", page=51,
                quoted_text="本报告期合计营业收入 408,548.46 万元",
                bound_context={"row_label": "本报告期合计"})
    base.update(kw)
    return Span(**base)


def _assertion(**kw) -> Assertion:
    base = dict(
        assertion_id="A-1", kind="financial_value", subject="启明信息", period="2023A",
        metric="营业收入", value="408548.46", unit="万元", raw_text="408,548.46",
        text="2023年本报告期合计营业收入408,548.46万元", source_spans=["S-1"],
    )
    base.update(kw)
    return Assertion(**base)


def _codes(outcome) -> list[str]:
    return [c["code"] for c in outcome.checks]


# ---------------------------------------------------------------------------
# 不变量 1：两轴分离
# ---------------------------------------------------------------------------


def test_two_axis_separation_bind_does_not_touch_evidence(kb):
    a = _assertion()
    assert a.evidence_status == EvidenceStatus.CANDIDATE.value

    out = bind(a, spans=[_span()], kb=kb)

    assert out.semantic_status == SemanticStatus.BOUND.value
    # bind 只管语义轴，绝不把候选直接标成已证。
    assert a.evidence_status == EvidenceStatus.CANDIDATE.value
    # 语义 bound 但证据未 attest → 仍不是事实。
    assert a.is_fact is False

    # 证据轴补齐后才成事实。
    a.evidence_status = EvidenceStatus.ATTESTED.value
    assert a.is_fact is True


def test_bind_writes_binding_checks_trace(kb):
    a = _assertion()
    bind(a, spans=[_span()], kb=kb)
    assert "binding_checks" in a.gate_trace
    assert [c["code"] for c in a.gate_trace["binding_checks"]][:4] == ["B1", "B2", "B3", "B4"]


# ---------------------------------------------------------------------------
# B1 概念绑定
# ---------------------------------------------------------------------------


def test_b1_hit_binds_and_fills_metric_id(kb):
    a = _assertion(metric_id="")
    out = bind(a, spans=[_span()], kb=kb)
    assert out.semantic_status == SemanticStatus.BOUND.value
    assert a.metric_id == "revenue"


def test_b1_miss_is_unbound(kb):
    a = _assertion(metric="星辰大海指标", metric_id="")
    out = bind(a, spans=[_span()], kb=kb)
    assert out.semantic_status == SemanticStatus.UNBOUND.value
    b1 = out.checks[0]
    assert b1["code"] == "B1" and b1["ok"] is False


def test_b1_unit_incompatible_is_conflicted(kb):
    # 营业收入基准是「元」，单位给「%」→ 值域/单位与概念定义冲突。
    a = _assertion(unit="%")
    out = bind(a, spans=[_span()], kb=kb)
    assert out.semantic_status == SemanticStatus.CONFLICTED.value


# ---------------------------------------------------------------------------
# B2 事件状态与时态
# ---------------------------------------------------------------------------


def test_b2_forecast_state_ok(kb):
    a = _assertion(kind="forecast", text="预测2023年本报告期合计营业收入408,548.46万元")
    out = bind(a, spans=[_span()], kb=kb)
    assert out.semantic_status == SemanticStatus.BOUND.value


def test_b2_forecast_marked_implemented_is_conflicted(kb):
    # 预测类不得标成已实施。
    a = _assertion(kind="forecast", text="2023年本报告期合计营业收入已实施408,548.46万元")
    out = bind(a, spans=[_span()], kb=kb)
    assert out.semantic_status == SemanticStatus.CONFLICTED.value


def test_b2_mixed_states_is_conflicted(kb):
    a = _assertion(text="2023年本报告期合计营业收入预案已实施408,548.46万元")
    out = bind(a, spans=[_span()], kb=kb)
    assert out.semantic_status == SemanticStatus.CONFLICTED.value


# ---------------------------------------------------------------------------
# B3 统计范围
# ---------------------------------------------------------------------------


def test_b3_undeterminable_is_unbound_not_defaulted(kb):
    # row_label 与 text 都没给出范围 → unbound，绝不默认成单一方案。
    a = _assertion(text="营业收入408,548.46万元")
    sp = _span(bound_context={"row_label": "营业收入"})
    out = bind(a, spans=[sp], kb=kb)
    assert out.semantic_status == SemanticStatus.UNBOUND.value
    b3 = [c for c in out.checks if c["code"] == "B3"][0]
    assert b3["ok"] is False


def test_b3_text_and_row_conflict_is_conflicted(kb):
    a = _assertion(text="多年累计营业收入408,548.46万元")
    sp = _span(bound_context={"row_label": "单一方案"})
    out = bind(a, spans=[sp], kb=kb)
    assert out.semantic_status == SemanticStatus.CONFLICTED.value


# ---------------------------------------------------------------------------
# B4 口径
# ---------------------------------------------------------------------------


def test_b4_consistent_caliber_binds(kb):
    a = _assertion(metric="归母净利润", metric_id="",
                   text="本报告期合计归母净利润408,548.46万元")
    sp = _span(bound_context={"caliber": "parent", "row_label": "本报告期合计"})
    out = bind(a, spans=[sp], kb=kb)
    assert out.semantic_status == SemanticStatus.BOUND.value


def test_b4_context_vs_text_conflict(kb):
    a = _assertion(metric="归母净利润", metric_id="",
                   text="本报告期合计归母净利润408,548.46万元")
    sp = _span(bound_context={"caliber": "扣非", "row_label": "本报告期合计"})
    out = bind(a, spans=[sp], kb=kb)
    assert out.semantic_status == SemanticStatus.CONFLICTED.value


# ---------------------------------------------------------------------------
# 不变量 2：权限不对称（正反两方向都钉）
# ---------------------------------------------------------------------------


def test_llm_can_lower_bound_to_conflicted(kb):
    # 方向 A：合法拉低。
    a = _assertion()
    out = bind(a, spans=[_span()], kb=kb, llm_verdict="conflicted")
    assert out.semantic_status == SemanticStatus.CONFLICTED.value
    assert a.semantic_status == SemanticStatus.CONFLICTED.value


def test_llm_cannot_raise_unbound_to_bound(kb):
    # 方向 B1：规则判 unbound，LLM 说 bound —— 必须仍然是 unbound。
    a = _assertion(metric="星辰大海指标", metric_id="")
    out = bind(a, spans=[_span()], kb=kb, llm_verdict="bound")
    assert out.semantic_status == SemanticStatus.UNBOUND.value


def test_llm_cannot_raise_conflicted_to_bound(kb):
    # 方向 B2：规则判 conflicted，LLM 说 bound —— 必须仍然是 conflicted。
    a = _assertion(kind="forecast", text="2023年本报告期合计营业收入已实施408,548.46万元")
    out = bind(a, spans=[_span()], kb=kb, llm_verdict="approve")
    assert out.semantic_status == SemanticStatus.CONFLICTED.value


def test_llm_bound_verdict_on_already_bound_stays_bound(kb):
    a = _assertion()
    out = bind(a, spans=[_span()], kb=kb, llm_verdict="bound")
    assert out.semantic_status == SemanticStatus.BOUND.value


def test_llm_verdict_recorded_when_it_refuses_raise(kb):
    a = _assertion(metric="星辰大海指标", metric_id="")
    out = bind(a, spans=[_span()], kb=kb, llm_verdict="bound")
    llm = [c for c in out.checks if c["code"] == "LLM"]
    assert llm and "抬成 bound" in llm[0]["why"]
