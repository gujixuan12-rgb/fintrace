"""硬边界 + 打转守门（对应 BUILD_SPEC §8 · v2.2 §4.1）。

硬边界只有否决权、没有说话权：Python（本模块）决定「还能不能继续」，
模型不能越过它。判定逻辑**纯函数**、**不抛异常**，只返回 ``BoundVerdict``。

打转五类（移植自 Hermes agent/tool_guardrails.py 的判定思路，MIT 声明见 docs/VENDOR.md）：
``exact_failure`` / ``same_tool_failure`` / ``idempotent_no_progress`` /
``identical_call_streak``（连续 3 次）/ ``identical_cycle``（周期 2~4，历史窗口 64）。

关键不变量：**一次成功的写入/执行会清零失败签名** —— 有进展就不算打转。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..contracts import BoundDecision, GuardCode

#: 打转历史窗口：只看最近这么多步（v2.2 §4.1）
HISTORY_WINDOW = 64
#: 「连续 3 次」里的 N
STREAK_N = 3
#: 周期检测的最小/最大周期
CYCLE_MIN, CYCLE_MAX = 2, 4
#: 接近预算上限的告警阈值（占上限比例）
WARN_RATIO = 0.8


@dataclass
class BoundVerdict:
    """一次边界判定的结果。

    ``decision`` ∈ ``contracts.BoundDecision``；``code`` ∈ ``ok`` /
    ``max_tool_calls`` / ``token_budget`` / ``deadline`` / ``cancelled`` /
    ``tool_permission`` / ``precondition`` / 五种 ``GuardCode``。
    """

    decision: str
    code: str
    reason: str = ""


# ---------------------------------------------------------------------------
# 步（step）读取辅助 —— 步由 loop 写入 state.steps，这里只读
# ---------------------------------------------------------------------------


def step_signature(step: dict) -> str:
    """一次调用的签名：``动作 + 参数``。参数用规范序列化，保证同调用同签名。"""
    return str(step.get("sig") or f"{step.get('action', '')}|{step.get('args_sig', '')}")


def _ok(step: dict) -> bool:
    return bool(step.get("ok"))


def _progress(step: dict) -> str:
    return str(step.get("progress") or "")


def _action(step: dict) -> str:
    return str(step.get("action") or "")


def _error(step: dict) -> str:
    return str(step.get("error") or "")


def _tail_since_success(steps: list[dict]) -> list[dict]:
    """取「最后一次成功之后」的尾部步序列。

    成功的写入/执行清零失败签名：更早的失败不计入失败类打转判定。
    """
    cut = 0
    for i, s in enumerate(steps):
        if _ok(s):
            cut = i + 1
    return steps[cut:]


# ---------------------------------------------------------------------------
# 五类打转检测
# ---------------------------------------------------------------------------


def _exact_failure(tail: list[dict]) -> str:
    """同一签名 + 同一错误文本，失败 ≥2 次 → exact_failure。"""
    seen: dict[tuple[str, str], int] = {}
    for s in tail:
        if _ok(s):
            continue
        key = (step_signature(s), _error(s))
        seen[key] = seen.get(key, 0) + 1
        if seen[key] >= 2:
            return f"同一调用 {key[0]} 两次失败且错误相同：{key[1]}"
    return ""


def _same_tool_failure(tail: list[dict]) -> str:
    """同一工具失败 ≥3 次（自该工具上次成功后计）→ same_tool_failure。"""
    per: dict[str, int] = {}
    for s in tail:
        if _ok(s):
            per.pop(_action(s), None)     # 该工具成功即清零
            continue
        a = _action(s)
        per[a] = per.get(a, 0) + 1
        if per[a] >= STREAK_N:
            return f"工具 {a} 连续失败 {per[a]} 次"
    return ""


def _idempotent_no_progress(steps: list[dict]) -> str:
    """同一调用成功但无进展，连续 ≥2 次（第 2 次即判）→ idempotent_no_progress。"""
    run = 0
    prev_sig = None
    for s in steps:
        sig = step_signature(s)
        if _ok(s) and _progress(s) == "none" and sig == prev_sig:
            run += 1
            if run >= 1:
                return f"同一调用 {sig} 连续 {run + 1} 次成功但无进展"
        else:
            run = 0
        prev_sig = sig
    return ""


def _identical_call_streak(steps: list[dict]) -> str:
    """同一调用连续出现 3 次（不论成败）→ identical_call_streak。"""
    run = 0
    prev = None
    for s in steps:
        sig = step_signature(s)
        if sig == prev:
            run += 1
            if run >= STREAK_N - 1:
                return f"同一调用 {sig} 连续出现 {run + 1} 次"
        else:
            run = 0
        prev = sig
    return ""


def _identical_cycle(steps: list[dict]) -> str:
    """末尾出现周期 2~4 的重复序列（模式至少完整重复两遍）→ identical_cycle。"""
    window = steps[-HISTORY_WINDOW:]
    sigs = [step_signature(s) for s in window]
    n = len(sigs)
    for k in range(CYCLE_MIN, CYCLE_MAX + 1):
        if n >= 2 * k and sigs[-k:] == sigs[-2 * k:-k]:
            # 排除「整段都是同一个签名」（那是 streak，不是 cycle）
            if len(set(sigs[-k:])) >= 2:
                return f"末尾出现周期 {k} 的重复调用序列：{sigs[-k:]}"
    return ""


def guard_checks(steps: list[dict]) -> list[BoundVerdict]:
    """纯函数：对步序列认五种打转，返回全部命中的 ``BoundVerdict``（decision=block）。

    先做失败清零（``_tail_since_success``），再逐类检测；同一序列可能命中多类，
    全部返回，调用方取第一条即可。
    """
    steps = list(steps or [])
    tail = _tail_since_success(steps)
    out: list[BoundVerdict] = []

    checks = (
        (GuardCode.EXACT_FAILURE, _exact_failure(tail)),
        (GuardCode.SAME_TOOL_FAILURE, _same_tool_failure(tail)),
        (GuardCode.IDEMPOTENT_NO_PROGRESS, _idempotent_no_progress(steps)),
        (GuardCode.IDENTICAL_CALL_STREAK, _identical_call_streak(steps)),
        (GuardCode.IDENTICAL_CYCLE, _identical_cycle(steps)),
    )
    for code, reason in checks:
        if reason:
            out.append(BoundVerdict(BoundDecision.BLOCK.value, code.value, reason))
    return out


# ---------------------------------------------------------------------------
# 硬边界判定
# ---------------------------------------------------------------------------


def budget_exhausted(state) -> bool:
    """额度是否已尽：工具调用数或 token 到顶。"""
    b = state.bounds
    cnt = state.counting or {}
    return (int(cnt.get("tool_calls", 0)) >= int(b.max_tool_calls)
            or int(cnt.get("tokens", 0)) >= int(b.token_budget))


def may_continue(state, *, action: str = "", now: float | None = None) -> BoundVerdict:
    """判定「还能不能继续」。只判定，不抛异常。

    检查顺序（先硬后软）：取消 → 截止日 → token 预算 → 工具配额 → 工具权限 →
    打转守门；接近上限时给 ``warn``。
    """
    b = state.bounds
    cnt = state.counting or {}

    if getattr(b, "cancel_requested", False):
        return BoundVerdict(BoundDecision.BLOCK.value, "cancelled", "收到取消标志，回路立即停止")

    deadline = float(getattr(b, "deadline_ts", 0.0) or 0.0)
    if deadline and now is not None and now > deadline:
        return BoundVerdict(BoundDecision.BLOCK.value, "deadline",
                            f"已过截止时间 {deadline}（now={now}）")

    if int(cnt.get("tokens", 0)) >= int(b.token_budget):
        return BoundVerdict(BoundDecision.BLOCK.value, "token_budget",
                            f"token 预算耗尽（{cnt.get('tokens')}/{b.token_budget}）")

    if int(cnt.get("tool_calls", 0)) >= int(b.max_tool_calls):
        return BoundVerdict(BoundDecision.BLOCK.value, "max_tool_calls",
                            f"工具调用配额耗尽（{cnt.get('tool_calls')}/{b.max_tool_calls}）")

    if action and action not in set(b.allowed_tools or ()):
        return BoundVerdict(BoundDecision.BLOCK.value, "tool_permission",
                            f"动作 {action} 不在允许工具集内")

    spins = guard_checks(state.steps or [])
    if spins:
        return spins[0]

    # 接近上限先给软告警（不阻断）
    calls = int(cnt.get("tool_calls", 0))
    if calls >= int(b.max_tool_calls * WARN_RATIO):
        return BoundVerdict(BoundDecision.WARN.value, "ok",
                            f"工具调用已用 {calls}/{b.max_tool_calls}，接近配额上限")
    return BoundVerdict(BoundDecision.ALLOW.value, "ok", "可继续")


__all__ = ["BoundVerdict", "may_continue", "guard_checks", "budget_exhausted",
           "step_signature", "HISTORY_WINDOW", "STREAK_N"]
