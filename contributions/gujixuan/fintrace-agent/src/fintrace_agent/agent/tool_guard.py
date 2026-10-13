"""工具守门：调用前的前置条件 + 调用后的进展判定（对应 BUILD_SPEC §8 · v2.2 §9.2）。

- ``before_call``：按工具写死前置条件，判定「这次调用放不放行」。
  它只做「参数与前置条件」这一层的再校验；硬边界（配额/权限/打转）归 bounds。
- ``after_call``：把一次收据翻成 ``ProgressKind``（进展四类 + none）。
  **被否决的证据也算进展**（排除错误假设本身就是有效调查）。

进展四类：``claim_status_change`` / ``hypothesis_eliminated`` / ``new_evidence`` /
``evidence_rejected``；都不是则 ``none``。
"""
from __future__ import annotations

from ..contracts import ProgressKind

NONE = ProgressKind.NONE.value
NEW_EVIDENCE = ProgressKind.NEW_EVIDENCE.value
EVIDENCE_REJECTED = ProgressKind.EVIDENCE_REJECTED.value
CLAIM_STATUS_CHANGE = ProgressKind.CLAIM_STATUS_CHANGE.value
HYPOTHESIS_ELIMINATED = ProgressKind.HYPOTHESIS_ELIMINATED.value


def _knowledge_ops() -> tuple:
    """t_knowledge 的 op 枚举：从工具模块取，避免两处定义走偏。"""
    try:
        from ..tools.t_knowledge import OPS

        return tuple(OPS)
    except Exception:                                            # noqa: BLE001
        return ("resolve_metric", "formula", "relation_rule", "context_check",
                "precision_for", "all_metrics")


def _has(state, attr: str) -> bool:
    """state 上某列表字段非空。"""
    seq = getattr(state, attr, None) or []
    return len(seq) > 0


def before_call(name: str, args: dict, state, *, kb, facts) -> tuple[bool, str]:
    """前置条件校验。返回 ``(是否放行, 理由)``。

    按工具写死（v2.2 §4.1 必要流程）：
    - ``t_compute``：需要相关证据已登记（``state.evidence_index`` 非空）；
    - ``t_knowledge``：需要查询词非空；
    - ``t_recall``：需要关键词或页范围；
    - ``t_extract``：需要目标材料已解析出 Span（证据里有 span）；
    - 其余：一律放行。
    """
    args = args or {}
    name = str(name or "")

    if name == "t_compute":
        if _has(state, "evidence_index"):
            return True, "ok"
        return False, "t_compute 前置条件不满足：尚无可复算的证据（先登记证据）"

    if name == "t_knowledge":
        # 工具真正的必填参数是 op（枚举），不是 query —— 照旧写会把模型的
        # 正确调用拦掉，或者放行一个注定 bad_args 的调用。
        if str(args.get("op") or "").strip():
            return True, "ok"
        return False, ("t_knowledge 前置条件不满足：缺少 op（可选："
                       + "、".join(_knowledge_ops()) + "）")

    if name == "t_recall":
        kws = args.get("keywords") or args.get("keyword") or args.get("query") or []
        if isinstance(kws, str):
            kws = [kws]
        has_kw = any(str(k).strip() for k in kws) if isinstance(kws, (list, tuple)) else bool(kws)
        has_span = bool(args.get("span_ids"))
        has_page = any(args.get(k) is not None
                       for k in ("page_from", "page_to", "page", "page_range"))
        if has_kw or has_span or has_page:
            return True, "ok"
        return False, ("t_recall 前置条件不满足：至少给 keywords / span_ids / 页码区间之一"
                       "（只给 limit 不算条件）")

    if name == "t_extract":
        if _has(state, "evidence_index"):
            return True, "ok"
        return False, ("t_extract 前置条件不满足：还没有任何原文片段被登记"
                       "——先用 t_parse 解析一份可用材料")

    return True, "ok"


def after_call(receipt, state) -> str:
    """把收据翻成 ``ProgressKind`` 值。判定有进展的优先级见下。"""
    # 1) 收据自带显式进展（工具最了解自己做了什么）
    explicit = str(getattr(receipt, "progress", "") or "")
    if explicit and explicit != NONE:
        return explicit

    result = getattr(receipt, "result", {}) or {}
    # 2) 收据结果里显式声明的语义进展
    if result.get("claim_status_changed"):
        return CLAIM_STATUS_CHANGE
    if result.get("hypothesis_eliminated"):
        return HYPOTHESIS_ELIMINATED
    # 3) 新增证据
    if getattr(receipt, "new_evidence", None):
        return NEW_EVIDENCE
    # 4) 证据被否决（也算进展）
    if getattr(receipt, "rejected", None):
        return EVIDENCE_REJECTED
    # 5) 调用失败且无任何登记 → 无进展
    return NONE


__all__ = ["before_call", "after_call", "NONE", "NEW_EVIDENCE", "EVIDENCE_REJECTED",
           "CLAIM_STATUS_CHANGE", "HYPOTHESIS_ELIMINATED"]
