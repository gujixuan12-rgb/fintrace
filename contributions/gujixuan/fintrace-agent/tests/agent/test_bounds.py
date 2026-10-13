"""agent/bounds —— 硬边界与打转五类（BUILD_SPEC §8 关键必测）。"""
from __future__ import annotations

from fintrace_agent.agent.bounds import guard_checks, may_continue
from fintrace_agent.contracts import BoundDecision, Bounds, GuardCode, TaskState


def mk_state(**kw) -> TaskState:
    bounds = kw.pop("bounds", None) or Bounds()
    counting = kw.pop("counting", None) or {"tool_calls": 0, "tokens": 0}
    return TaskState(run_id="FT-x", bounds=bounds, counting=counting, **kw)


def step(action, args=None, *, ok=True, error="", progress="none"):
    return {"action": action, "args": args or {}, "ok": ok, "error": error, "progress": progress}


def codes(steps):
    return {v.code for v in guard_checks(steps)}


# ---------------------------------------------------------------------------
# 打转五类各一例
# ---------------------------------------------------------------------------


def test_exact_failure():
    """同一调用、同一错误，失败两次 → exact_failure。"""
    steps = [step("t_compute", {"x": 1}, ok=False, error="boom"),
             step("t_compute", {"x": 1}, ok=False, error="boom")]
    assert GuardCode.EXACT_FAILURE.value in codes(steps)
    # 只两次同名失败，不该误判同工具连续失败（需要 3 次）
    assert GuardCode.SAME_TOOL_FAILURE.value not in codes(steps)


def test_same_tool_failure():
    """同一工具失败三次（参数与错误各不相同）→ same_tool_failure。"""
    steps = [step("t_compute", {"x": 1}, ok=False, error="e1"),
             step("t_compute", {"x": 2}, ok=False, error="e2"),
             step("t_compute", {"x": 3}, ok=False, error="e3")]
    assert GuardCode.SAME_TOOL_FAILURE.value in codes(steps)
    assert GuardCode.EXACT_FAILURE.value not in codes(steps)


def test_idempotent_no_progress():
    """同一调用成功但无进展，连续两次 → idempotent_no_progress（不误判 streak）。"""
    steps = [step("t_recall", {"a": 1}, ok=True, progress="none"),
             step("t_recall", {"a": 1}, ok=True, progress="none")]
    assert GuardCode.IDEMPOTENT_NO_PROGRESS.value in codes(steps)
    assert GuardCode.IDENTICAL_CALL_STREAK.value not in codes(steps)


def test_identical_call_streak():
    """同一调用连续 3 次（有进展）→ identical_call_streak。"""
    steps = [step("t_recall", {"a": 1}, ok=True, progress="new_evidence")] * 3
    assert GuardCode.IDENTICAL_CALL_STREAK.value in codes(steps)
    assert GuardCode.IDEMPOTENT_NO_PROGRESS.value not in codes(steps)


def test_identical_cycle():
    """末尾出现周期 2 的重复序列 → identical_cycle。"""
    a = step("t_recall", {"a": 1}, ok=True, progress="new_evidence")
    b = step("t_knowledge", {"q": "x"}, ok=True, progress="new_evidence")
    steps = [a, b, a, b]
    assert GuardCode.IDENTICAL_CYCLE.value in codes(steps)
    assert GuardCode.IDENTICAL_CALL_STREAK.value not in codes(steps)


def test_success_clears_failure_signature():
    """一次成功写入/执行要清零失败签名：成功之后不再算失败类打转。"""
    fail_a = step("t_compute", {"x": 1}, ok=False, error="boom")
    steps = [fail_a, dict(fail_a)]
    assert GuardCode.EXACT_FAILURE.value in codes(steps)

    steps.append(step("t_compute", {"x": 9}, ok=True, progress="new_evidence"))
    cleared = codes(steps)
    assert GuardCode.EXACT_FAILURE.value not in cleared
    assert GuardCode.SAME_TOOL_FAILURE.value not in cleared


# ---------------------------------------------------------------------------
# 硬边界
# ---------------------------------------------------------------------------


def test_cancel_blocks():
    st = mk_state(bounds=Bounds(cancel_requested=True))
    v = may_continue(st, action="")
    assert v.decision == BoundDecision.BLOCK.value
    assert v.code == "cancelled"


def test_token_budget_blocks():
    st = mk_state(bounds=Bounds(token_budget=100), counting={"tool_calls": 0, "tokens": 100})
    v = may_continue(st)
    assert v.decision == BoundDecision.BLOCK.value
    assert v.code == "token_budget"


def test_max_tool_calls_blocks():
    st = mk_state(bounds=Bounds(max_tool_calls=5), counting={"tool_calls": 5, "tokens": 0})
    v = may_continue(st)
    assert v.decision == BoundDecision.BLOCK.value
    assert v.code == "max_tool_calls"


def test_tool_permission_blocks():
    """允许集之外的（这里 t_author_tool 默认关闭）→ BLOCK。"""
    st = mk_state()
    assert "t_author_tool" not in st.bounds.allowed_tools
    v = may_continue(st, action="t_author_tool")
    assert v.decision == BoundDecision.BLOCK.value
    assert v.code == "tool_permission"


def test_deadline_blocks():
    st = mk_state(bounds=Bounds(deadline_ts=1.0))
    v = may_continue(st, now=2.0)
    assert v.decision == BoundDecision.BLOCK.value
    assert v.code == "deadline"


def test_fresh_state_allows_and_near_budget_warns():
    st = mk_state(bounds=Bounds(max_tool_calls=10), counting={"tool_calls": 0, "tokens": 0})
    assert may_continue(st).decision == BoundDecision.ALLOW.value

    st2 = mk_state(bounds=Bounds(max_tool_calls=10), counting={"tool_calls": 8, "tokens": 0})
    assert may_continue(st2).decision == BoundDecision.WARN.value


def test_may_continue_surfaces_spin_as_block():
    """打转命中时 may_continue 直接 BLOCK（边界接管）。"""
    st = mk_state()
    st.steps = [step("t_recall", {"a": 1}, ok=True, progress="new_evidence")] * 3
    v = may_continue(st)
    assert v.decision == BoundDecision.BLOCK.value
    assert v.code == GuardCode.IDENTICAL_CALL_STREAK.value
