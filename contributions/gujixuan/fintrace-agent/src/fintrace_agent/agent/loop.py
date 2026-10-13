"""回路主干（对应 BUILD_SPEC §8 · v2.2 §4.3）。

流程严格按 v2.2 §4.3：
``bounds.may_continue`` → ``build_context`` → ``decide`` → ``tool_guard.before_call``
→ ``execute`` → W 门 → 原子写 ``state`` + 追加 ``decisions.jsonl`` → 回环。

**出口只有三个**（全部由 Python 判定）：
(a) ``bounds``（硬边界 block）；
(b) ``stop``（模型请求 stop **且 V 门认可**）；
(c) ``budget_exhausted``（V 门要求补查但额度/步数已尽 → 带残余出报告）。
另有一个非「正当出口」的回落：连续 3 次非法决策 → ``fixed``。**模型说完了不等于完。**

依赖注入（都放 ``tools_ctx`` 里，缺省时惰性 import，导入失败不影响测试）：
- ``execute(name, args, ctx)``：工具执行器（缺省惰性 import ``tools.registry.execute``）；
- ``fixed_runner(case, state)``：落回固定链路时的执行器（缺省惰性 import ``cases.fixed_runner``）；
- ``validator(receipt, state)``：W 门（缺省把收据里的 new_evidence / rejected 登记）；
- ``gate(findings, state)`` 或带 ``review`` 的对象：V 门（缺省降级放行）；
- ``leads``：待按 as_of 过滤的联网线索。
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field

from ..contracts import (
    AsOfMode,
    BoundDecision,
    Bounds,
    Claim,
    ClaimStatus,
    Decision,
    Finding,
    TaskState,
    ToolReceipt,
    Verdict,
    canonical_json,
    digest,
)
from ..store.evidence import EvidenceLog
from ..store.runs import append_jsonl, new_run_id
from . import bounds as _bounds
from . import planner as _planner
from . import tool_guard as _tool_guard
from .state import StateStore

#: 连续非法决策多少次 → 回落 fixed（v2.2 §4.3）
MAX_INVALID_DECISIONS = 3


@dataclass
class LoopResult:
    state: TaskState
    findings: list = field(default_factory=list)
    gate: object | None = None
    decisions: list = field(default_factory=list)
    exit: str = ""          # "stop" / "bounds" / "budget_exhausted" / "fixed"


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def _case_field(case, name: str, default=""):
    if case is None:
        return default
    if isinstance(case, dict):
        return case.get(name, default)
    return getattr(case, name, default)


def _step_record(decision: Decision, *, ok: bool, error: str, progress: str,
                 receipt: ToolReceipt | None = None) -> dict:
    args_sig = canonical_json(decision.args or {})
    return {
        "n": decision.n,
        "action": decision.action,
        "args": decision.args or {},
        "args_sig": args_sig,
        "sig": f"{decision.action}|{args_sig}",
        "ok": bool(ok),
        "error": error or "",
        # 报错的具体文字必须留住：只回一个 bad_args，模型下一轮只能接着猜参数。
        "message": str((getattr(receipt, "result", {}) or {}).get("message", ""))[:400]
        if receipt else "",
        "result_keys": sorted((getattr(receipt, "result", {}) or {}).keys())[:12]
        if receipt else [],
        "progress": progress,
        "new_evidence": len(getattr(receipt, "new_evidence", []) or []) if receipt else 0,
        "at": _now(),
    }


def _append_unique(seq: list, items) -> None:
    for it in items or []:
        if it and it not in seq:
            seq.append(it)


# ---------------------------------------------------------------------------
# 依赖解析（全部惰性、包 try/except）
# ---------------------------------------------------------------------------


def _default_execute(name: str, args: dict, ctx) -> ToolReceipt:
    """缺省执行器：惰性 import ``tools.registry``；导入失败返回明确失败的收据。"""
    try:
        from ..tools import registry
    except Exception:
        return ToolReceipt(tool=name, ok=False, error="tool_layer_unavailable")
    try:
        return registry.execute(name, args, ctx)
    except Exception as e:
        return ToolReceipt(tool=name, ok=False, error=f"execute_error:{type(e).__name__}")


def _resolve_execute(tools_ctx: dict):
    fn = (tools_ctx or {}).get("execute")
    return fn if callable(fn) else _default_execute


def _resolve_fixed(tools_ctx: dict):
    fn = (tools_ctx or {}).get("fixed_runner")
    if callable(fn):
        return fn
    try:
        from ..cases import fixed_runner as _fr
        return getattr(_fr, "run_fixed", None)
    except Exception:
        return None


def _resolve_validator(tools_ctx: dict):
    fn = (tools_ctx or {}).get("validator")
    return fn if callable(fn) else None


def _resolve_gate(tools_ctx: dict):
    return (tools_ctx or {}).get("gate")


# ---------------------------------------------------------------------------
# W 门 / V 门（缺省降级）
# ---------------------------------------------------------------------------


def _admit(receipt: ToolReceipt, state, validator) -> tuple[list[str], list[dict]]:
    """过 W 门，返回 ``(accepted ids, rejected 明细)``。缺省校验器直接采信收据。"""
    if validator is not None:
        try:
            res = validator(receipt, state)
        except Exception:
            res = None
        if res is not None:
            acc = getattr(res, "accepted", None)
            if acc is None and isinstance(res, dict):
                acc = res.get("accepted")
            new_ids = []
            for a in acc or []:
                new_ids.append(getattr(a, "assertion_id", None) or getattr(a, "span_id", None) or str(a))
            rej = getattr(res, "rejected", None)
            if rej is None and isinstance(res, dict):
                rej = res.get("rejected")
            return [i for i in new_ids if i], list(rej or [])
    return list(receipt.new_evidence or []), list(receipt.rejected or [])


def _run_gate(findings, state, gate):
    """跑 V 门。未注入 gate 时降级为「放行」，但**出口结构不变**（仍要经过这里）。"""
    if gate is None:
        return {"approve": True, "residuals": [], "note": "V 门未注入，降级放行"}
    try:
        if callable(gate):
            return gate(findings, state)
        review = getattr(gate, "review", None)
        if callable(review):
            return review(findings)
    except Exception as e:
        return {"approve": False, "needs_more": True, "residuals": [],
                "error": f"{type(e).__name__}: {e}"}
    return {"approve": True}


def _v_approves(vres) -> bool:
    """V 门是否认可收尾。"""
    if vres is None:
        return True
    if isinstance(vres, dict):
        if "approve" in vres:
            return bool(vres["approve"])
        if vres.get("needs_more"):
            return False
        return True
    if hasattr(vres, "approve"):
        return bool(vres.approve)
    counts = getattr(vres, "counts", None)
    if isinstance(counts, dict):
        return int(counts.get("blocked", 0)) == 0 and int(counts.get("manual_review", 0)) == 0
    return True


def _build_findings(state: TaskState) -> list[Finding]:
    """从已解决主张生成 Finding（供 V 门逐条检查）。"""
    verdict_of = {
        ClaimStatus.VERIFIED.value: Verdict.SUPPORTED.value,
        ClaimStatus.INSUFFICIENT.value: Verdict.INSUFFICIENT.value,
        ClaimStatus.BLOCKED.value: Verdict.INSUFFICIENT.value,
    }
    out: list[Finding] = []
    for i, claim in enumerate(state.claims):
        verdict = verdict_of.get(claim.status)
        if verdict is None:
            continue
        out.append(Finding(finding_id=f"F-{claim.claim_id or i + 1}",
                           claim_id=claim.claim_id, verdict=verdict))
    return out


# ---------------------------------------------------------------------------
# fixed 回落
# ---------------------------------------------------------------------------


def _finish_fixed(case, state: TaskState, decisions, findings, gate_res, tools_ctx, store) -> LoopResult:
    runner = _resolve_fixed(tools_ctx)
    result = None
    if runner is not None:
        try:
            result = runner(case, state)
        except Exception as e:
            result = {"error": f"{type(e).__name__}: {e}"}
    state.mode = "fixed"
    store.save(state)
    out = LoopResult(state=state, findings=findings, gate=gate_res,
                     decisions=decisions, exit="fixed")
    out.fixed_result = result       # 非契约属性，仅便于调用方复用基线结果
    return out


# ---------------------------------------------------------------------------
# 主回路
# ---------------------------------------------------------------------------


def run_loop(*, case, client=None, mode: str = "agent", max_steps: int | None = None,
             run_id: str = "", kb=None, facts=None, tools_ctx=None) -> LoopResult:
    """跑一次回路。参数与返回值见模块文档。"""
    tools_ctx = dict(tools_ctx or {})
    case_id = str(_case_field(case, "case_id", "") or "")
    run_id = (run_id or "").strip() or new_run_id(case_id)
    mode = mode or "agent"

    store = StateStore(run_id)
    # 证据日志必须与工具层用**同一个实例**：各自 new 一个的话，回路读到的内存索引
    # 永远是刚启动时那份（空的），于是模型会被告知「parsed_docs 为空」，
    # 明明刚解析完又回去重复解析（真跑过一轮才发现）。
    evidence = tools_ctx.get("evidence") or EvidenceLog(run_id)

    as_of_mode = str(_case_field(case, "as_of_mode",
                                 AsOfMode.HISTORICAL_REPLAY.value) or AsOfMode.HISTORICAL_REPLAY.value)
    cutoff = str(_case_field(case, "prediction_cutoff_date", "")
                 or _case_field(case, "cutoff_date", "") or "")
    hard = Bounds(cutoff_date=cutoff, as_of_mode=as_of_mode)

    goal = {"task": "audit_draft", "case_id": case_id,
            "company": str(_case_field(case, "company", "") or ""),
            "period": str(_case_field(case, "period", "") or "")}
    state = TaskState(run_id=run_id, mode=mode, goal=goal, bounds=hard, as_of=cutoff)
    state.artifact_fingerprint = digest(goal)
    for c in (_case_field(case, "claims", []) or []):
        state.claims.append(c if isinstance(c, Claim) else Claim.from_dict(c))
    store.save(state)

    decisions: list[Decision] = []
    findings: list[Finding] = []
    gate_res = None

    # fixed 模式：直接走固定链路
    if mode == "fixed":
        return _finish_fixed(case, state, decisions, findings, gate_res, tools_ctx, store)

    # agent 模式但模型未配置 → 回落 fixed（无 key 也要能跑）
    if client is None or not bool(getattr(client, "configured", True)):
        return _finish_fixed(case, state, decisions, findings, gate_res, tools_ctx, store)

    execute = _resolve_execute(tools_ctx)
    validator = _resolve_validator(tools_ctx)
    gate = _resolve_gate(tools_ctx)
    leads = tools_ctx.get("leads")
    decisions_path = store.dir / "decisions.jsonl"

    limit = int(max_steps) if max_steps else int(hard.max_tool_calls)
    limit = max(1, limit)
    consecutive_invalid = 0
    exit_ = ""

    for _ in range(limit):
        # (3) 硬边界
        bv = _bounds.may_continue(state, action="", now=_dt.datetime.now().timestamp())
        if bv.decision == BoundDecision.BLOCK.value:
            exit_ = "bounds"
            break

        # (4)(5) 组装上下文 → 调模型
        context = _planner.build_context(
            state, evidence=evidence, kb=kb, fact_store=facts,
            as_of_mode=as_of_mode, leads=leads,
            documents=_case_field(case, "documents", []) or [],
            tools=_planner.tool_manual(tools_ctx))
        decision = _planner.decide(state, client, context=context, prompt_version="p1")
        decision.n = len(decisions) + 1
        decisions.append(decision)

        usage = getattr(decision, "usage", {}) or {}
        state.counting["tokens"] = int(state.counting.get("tokens", 0)) + int(
            usage.get("total_tokens", 0) or 0)

        # (9) 决策日志：含模型原文响应 / state_digest / as_of / token 用量（可重放、可算账）
        _row = decision.to_dict()
        if usage:
            _row["usage"] = dict(usage)
        _row["tokens_total_so_far"] = int(state.counting.get("tokens", 0) or 0)
        append_jsonl(decisions_path, _row)

        # 非法决策 → 计数；连续 3 次 → 回落 fixed
        if not decision.valid:
            consecutive_invalid += 1
            state.counting["invalid_decisions"] = int(state.counting.get("invalid_decisions", 0)) + 1
            store.save(state)
            if consecutive_invalid >= MAX_INVALID_DECISIONS:
                findings = _build_findings(state) or findings
                return _finish_fixed(case, state, decisions, findings, gate_res, tools_ctx, store)
            continue
        consecutive_invalid = 0

        # 模型请求 stop → 必须过 V 门才算出口 (b)
        if decision.action == "stop":
            state.stop = {"requested_by_model": True, "reason": decision.why}
            findings = _build_findings(state)
            gate_res = _run_gate(findings, state, gate)
            if _v_approves(gate_res):
                exit_ = "stop"
                store.save(state)
                break
            if _bounds.budget_exhausted(state):
                exit_ = "budget_exhausted"
                store.save(state)
                break
            # V 门要求补查且额度未尽 → 清掉 stop，继续调查（不是第四个出口）
            state.stop = {"requested_by_model": False, "reason": "V 门要求补查，继续调查"}
            store.save(state)
            continue

        # 工具权限（动作级二次确认）
        if decision.action not in set(hard.allowed_tools or ()):
            decision.bound_decision = BoundDecision.BLOCK.value
            state.steps.append(_step_record(decision, ok=False, error="tool_permission",
                                            progress=_tool_guard.NONE))
            store.save(state)
            exit_ = "bounds"
            break

        # (6) 前置条件
        ok, why = _tool_guard.before_call(decision.action, decision.args, state, kb=kb, facts=facts)
        if not ok:
            decision.bound_decision = BoundDecision.BLOCK.value
            state.steps.append(_step_record(decision, ok=False, error=f"precondition:{why}",
                                            progress=_tool_guard.NONE))
            store.save(state)
            continue

        # (7) 执行
        receipt = execute(decision.action, decision.args, tools_ctx)
        if not isinstance(receipt, ToolReceipt):                    # 容错：dict 也能用
            receipt = ToolReceipt(**receipt) if isinstance(receipt, dict) else ToolReceipt(
                tool=decision.action, ok=False, error="bad_receipt")
        progress = _tool_guard.after_call(receipt, state)
        receipt.progress = progress

        # (8) W 门：登记 / 否决
        accepted, rejected = _admit(receipt, state, validator)
        _append_unique(state.evidence_index, accepted)
        _append_unique(state.rejected_index, [r.get("span_sha", "") or r.get("raw", "")
                                              for r in rejected] if rejected else [])

        decision.bound_decision = BoundDecision.ALLOW.value
        state.counting["tool_calls"] = int(state.counting.get("tool_calls", 0)) + 1
        state.steps.append(_step_record(decision, ok=receipt.ok, error=receipt.error,
                                        progress=progress, receipt=receipt))
        # (9) 原子写 state + 追加 decisions.jsonl（上面已追加）
        store.save(state)

    # 收尾：跑 V 门（stop 出口已跑过）
    if not findings:
        findings = _build_findings(state)
    if gate_res is None:
        gate_res = _run_gate(findings, state, gate)
    store.save(state)

    if not exit_:
        exit_ = "budget_exhausted"      # 走完步数上限 / V 要求补查但无更多额度
    return LoopResult(state=state, findings=findings, gate=gate_res,
                      decisions=decisions, exit=exit_)


__all__ = ["LoopResult", "run_loop", "MAX_INVALID_DECISIONS"]
