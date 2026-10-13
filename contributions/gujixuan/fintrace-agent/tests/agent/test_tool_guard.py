"""agent/tool_guard —— 前置条件与进展四类（BUILD_SPEC §8）。"""
from __future__ import annotations

from fintrace_agent.agent.tool_guard import after_call, before_call
from fintrace_agent.contracts import ProgressKind, TaskState, ToolReceipt


def mk_state(evidence=None) -> TaskState:
    return TaskState(run_id="FT-tg", evidence_index=evidence or [])


def test_t_knowledge_requires_op_not_query():
    """``t_knowledge`` 的真实必填参数是 ``op``（枚举），**不是** ``query``。

    原来的测试断言 ``{"query": "净利润"}`` 能放行 —— 那是个假放行：工具根本没有
    query 参数，放过去只会拿到 ``bad_args``。真模型跑一轮才发现守门与工具在互相打架。
    """
    assert before_call("t_knowledge", {}, mk_state(), kb=None, facts=None)[0] is False
    assert before_call("t_knowledge", {"op": "  "}, mk_state(), kb=None, facts=None)[0] is False
    assert before_call("t_knowledge", {"query": "净利润"}, mk_state(), kb=None, facts=None)[0] is False
    assert before_call("t_knowledge", {"op": "resolve_metric", "name": "净利润"},
                       mk_state(), kb=None, facts=None)[0] is True


def test_t_knowledge_block_reason_lists_valid_ops():
    """拦住的同时要把「可选 op」告诉模型，否则它下一轮还是猜。"""
    ok, why = before_call("t_knowledge", {}, mk_state(), kb=None, facts=None)
    assert ok is False and "resolve_metric" in why


def test_t_recall_accepts_real_param_names():
    """``t_recall`` 的真实参数是 ``keywords`` / ``span_ids`` / ``page_from`` / ``page_to``。

    只给 ``limit`` 不算条件（真模型就这么调过一次，白费一步）。
    """
    assert before_call("t_recall", {}, mk_state(), kb=None, facts=None)[0] is False
    assert before_call("t_recall", {"limit": 50}, mk_state(), kb=None, facts=None)[0] is False
    assert before_call("t_recall", {"keywords": ["分红"]}, mk_state(), kb=None, facts=None)[0] is True
    assert before_call("t_recall", {"keywords": ["  "]}, mk_state(), kb=None, facts=None)[0] is False
    assert before_call("t_recall", {"span_ids": ["sp-1"]}, mk_state(), kb=None, facts=None)[0] is True
    assert before_call("t_recall", {"page_from": 1, "page_to": 3},
                       mk_state(), kb=None, facts=None)[0] is True
    # 旧参数名仍兼容：改名不该把别处还在用的调用拦掉
    assert before_call("t_recall", {"keyword": "分红"}, mk_state(), kb=None, facts=None)[0] is True
    assert before_call("t_recall", {"page": 3}, mk_state(), kb=None, facts=None)[0] is True


def test_t_compute_requires_registered_evidence():
    assert before_call("t_compute", {}, mk_state(), kb=None, facts=None)[0] is False
    assert before_call("t_compute", {}, mk_state(["A-1"]), kb=None, facts=None)[0] is True


def test_t_extract_requires_parsed_spans():
    assert before_call("t_extract", {}, mk_state(), kb=None, facts=None)[0] is False
    assert before_call("t_extract", {}, mk_state(["sp-1"]), kb=None, facts=None)[0] is True


def test_unknown_tool_allowed_by_guard():
    assert before_call("t_lookup", {}, mk_state(), kb=None, facts=None)[0] is True


# ---------------------------------------------------------------------------
# after_call：进展四类 + none
# ---------------------------------------------------------------------------


def test_after_call_new_evidence():
    r = ToolReceipt(tool="t_parse", ok=True, new_evidence=["sp-1"])
    assert after_call(r, mk_state()) == ProgressKind.NEW_EVIDENCE.value


def test_after_call_evidence_rejected_is_progress():
    r = ToolReceipt(tool="t_extract", ok=True, rejected=[{"code": "W2", "raw": "x"}])
    assert after_call(r, mk_state()) == ProgressKind.EVIDENCE_REJECTED.value


def test_after_call_none_when_nothing_happened():
    r = ToolReceipt(tool="t_parse", ok=False, error="boom")
    assert after_call(r, mk_state()) == ProgressKind.NONE.value


def test_after_call_explicit_progress_wins():
    r = ToolReceipt(tool="t_compute", ok=True, progress=ProgressKind.CLAIM_STATUS_CHANGE.value)
    assert after_call(r, mk_state()) == ProgressKind.CLAIM_STATUS_CHANGE.value


def test_after_call_hypothesis_eliminated_from_result():
    r = ToolReceipt(tool="t_compute", ok=True, result={"hypothesis_eliminated": "H-1"})
    assert after_call(r, mk_state()) == ProgressKind.HYPOTHESIS_ELIMINATED.value


def test_before_call_returns_reason_string():
    ok, why = before_call("t_knowledge", {}, mk_state(), kb=None, facts=None)
    assert ok is False and isinstance(why, str) and why
