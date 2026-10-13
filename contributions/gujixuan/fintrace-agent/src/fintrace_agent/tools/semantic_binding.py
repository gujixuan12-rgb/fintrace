"""独立语义绑定检查器（B1~B4）—— 模块 E 的一半，对应 BUILD_SPEC 第 5 节与方案 v2.2 §7。

这道门回答的是「**理解对不对**」，与 W 门（「证据真不真」）是**两条独立的轴**：

* 过了 W 的断言只到 ``evidence_status=attested``，``semantic_status`` 仍是 ``unbound``；
* 本模块把 ``semantic_status`` 推到 ``bound`` / ``conflicted`` / 保持 ``unbound``；
* 两轴都过（``attested`` + ``bound``）才算事实（``Assertion.is_fact``）。**本模块绝不改
  ``evidence_status``。**

四项检查：

- **B1 概念绑定**：指标名必须解析到知识库里的标准概念（别名/简写/同义命中），不是字符串相似。
  命中失败 → ``unbound``；命中但值域/单位与概念定义冲突 → ``conflicted``。
- **B2 事件状态与时态**：``已实施 / 已审议通过 / 预案 / 计划 / 预测`` 五类不得混用；
  预测类（``kind=forecast``）不得标成已实施。
- **B3 统计范围**：``单一方案 / 本报告期合计 / 多年累计``，从 ``bound_context.row_label`` 与
  ``text`` 判定；**判定不出 → ``unbound``（绝不默认成单一方案）**；两处判出不同 → ``conflicted``。
- **B4 口径**：``合并 / 母公司 / 扣非``，``bound_context`` 与 ``text``（及 ``assertion.caliber``）
  冲突 → ``conflicted``。

**权限不对称（v2.2 §7）**：``llm_verdict`` 只允许把结果**拉低**（``bound`` → ``conflicted``），
**不允许**把 ``unbound`` / ``conflicted`` 抬成 ``bound``。批准权在规则与知识库手里。

只用标准库；数据类一律来自 ``fintrace_agent.contracts``。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from ..contracts import Assertion, Origin, SemanticStatus

#: B2 事件状态受控词表（与 knowledge/data/contexts.yaml 的 event_state 一致）。
_EVENT_STATES: tuple[str, ...] = ("已实施", "已审议通过", "预案", "计划", "预测")

#: 事件状态 → 时态类别。已实施/已审议通过 = 已落地；预案/计划 = 计划中；预测 = 预测。
_STATE_CATEGORY: dict[str, str] = {
    "已实施": "realized",
    "已审议通过": "realized",
    "预案": "planned",
    "计划": "planned",
    "预测": "predicted",
}

#: 各 kind 期望的时态类别；不在表里的 kind 不做时态约束。
_KIND_CATEGORY: dict[str, str] = {
    "financial_value": "realized",
    "operating_event": "realized",
    "fact_statement": "realized",
    "forecast": "predicted",
}

#: B3 统计范围关键词，按优先级从高到低（多 > 本报告期 > 单一）。
_SCOPE_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("多年累计", ("多年累计", "历年累计", "多年", "累计")),
    ("本报告期合计", ("本报告期合计", "本报告期", "报告期合计", "本期合计", "合计", "总计")),
    ("单一方案", ("单一方案", "单个方案", "独立方案", "单一", "本方案", "该方案", "每10股", "每股")),
)

#: B4 口径归一。
_CALIBER_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("deducted", ("扣除非经常性损益", "扣非")),
    ("parent", ("归母", "归属于上市公司股东", "归属于母公司", "母公司")),
    ("consolidated", ("合并",)),
)

#: llm_verdict 里表示「拉低」的词（英文 / 中文）。
_LLM_LOWER: frozenset[str] = frozenset({
    "conflicted", "conflict", "reject", "rejected", "suspect", "doubt",
    "incorrect", "wrong", "flag", "warn", "uncertain", "不通过", "存疑", "冲突",
})
#: llm_verdict 里表示「批准」的词；一律**不接受**（不能把结果抬成 bound）。
_LLM_RAISE: frozenset[str] = frozenset({
    "bound", "bind", "approve", "approved", "accept", "accepted",
    "ok", "pass", "yes", "true", "通过", "绑定", "批准",
})


@dataclass
class BindingOutcome:
    """一次语义绑定的结果。"""

    assertion: Assertion
    checks: list[dict] = field(default_factory=list)   # [{"code":"B1","ok":bool,"why":str}]
    semantic_status: str = SemanticStatus.UNBOUND.value


@dataclass
class _Chk:
    """单项检查的内部结果。``kind`` ∈ {``""``, ``unbound``, ``conflict``}。"""

    code: str
    ok: bool
    kind: str
    why: str


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _dec(text: str | None) -> Decimal | None:
    """把「可含千分位的数字串」解析成 Decimal；解析不了返回 None。"""
    if text is None:
        return None
    t = str(text).replace(",", "").strip()
    if not t:
        return None
    try:
        return Decimal(t)
    except InvalidOperation:
        return None


def _first_span(assertion: Assertion, spans_by_id: dict) -> object | None:
    """取该断言引用的第一个 Span（按 source_spans 顺序）。"""
    for sid in assertion.source_spans or []:
        span = spans_by_id.get(sid)
        if span is not None:
            return span
    return None


def _classify_scope(text: str | None) -> str:
    """把文本归一到 B3 统计范围之一；判不出返回空串。"""
    t = text or ""
    for scope, kws in _SCOPE_KEYWORDS:
        if any(kw in t for kw in kws):
            return scope
    return ""


def _classify_caliber(text: str | None) -> str:
    """把文本归一到 B4 口径之一；判不出返回空串。"""
    t = text or ""
    for caliber, kws in _CALIBER_PATTERNS:
        if any(kw in t for kw in kws):
            return caliber
    return ""


# ---------------------------------------------------------------------------
# B1~B4
# ---------------------------------------------------------------------------


def _check_b1(assertion: Assertion, kb) -> _Chk:
    """B1 概念绑定：解析 + 值域/单位一致性。"""
    concept = None
    if (assertion.metric or "").strip():
        concept = kb.resolve_metric(assertion.metric)
    if concept is None and (assertion.metric_id or "").strip():
        concept = kb.resolve_metric(assertion.metric_id)
    if concept is None:
        return _Chk("B1", False, "unbound",
                    f"指标名 {assertion.metric or assertion.metric_id!r} 无法解析到标准概念")

    metric_id = concept["metric_id"]
    base_unit = concept.get("base_unit") or ""
    unit = (assertion.unit or "").strip()
    if unit:
        p = kb.precision_for(metric_id, unit)
        if p is None:
            return _Chk("B1", False, "conflict",
                        f"单位 {unit} 不在受控单位表，与概念 {metric_id} 不兼容")
        if base_unit and p.get("base") and p["base"] != base_unit:
            return _Chk("B1", False, "conflict",
                        f"单位 {unit} 的基准 {p['base']} 与概念 {metric_id} 的基准 {base_unit} 不一致")

    # 值域：财务数值必须是可解析的数字。
    if assertion.kind == "financial_value" and (assertion.value or "").strip():
        if _dec(assertion.value) is None:
            return _Chk("B1", False, "conflict",
                        f"值 {assertion.value!r} 不是数字，与财务数值概念的值域冲突")

    # 命中即回填标准 metric_id（只补空，不覆盖）。
    if not (assertion.metric_id or "").strip():
        assertion.metric_id = metric_id
    return _Chk("B1", True, "", f"指标名解析到标准概念 {metric_id}")


def _check_b2(assertion: Assertion) -> _Chk:
    """B2 事件状态与时态：五类不得混用，预测类不得标成已实施。"""
    text = assertion.text or ""
    present = [s for s in _EVENT_STATES if s in text]
    uniq = list(dict.fromkeys(present))
    if len(uniq) >= 2:
        return _Chk("B2", False, "conflict", f"同句出现多个事件状态：{'/'.join(uniq)}")

    if not uniq:
        return _Chk("B2", True, "", "无事件状态词，不做时态约束")

    state = uniq[0]
    cat = _STATE_CATEGORY[state]
    expect = _KIND_CATEGORY.get(assertion.kind, "")
    if expect and cat != expect:
        return _Chk("B2", False, "conflict",
                    f"事件状态「{state}」（{cat}）与 kind={assertion.kind}（期望 {expect}）不一致")
    if state in ("已实施", "已审议通过") and assertion.origin != Origin.MATERIAL.value:
        return _Chk("B2", False, "conflict",
                    f"来源 origin={assertion.origin} 不能断言已落地事件「{state}」")
    return _Chk("B2", True, "", f"事件状态「{state}」与 kind={assertion.kind} 一致")


def _check_b3(assertion: Assertion, spans_by_id: dict) -> _Chk:
    """B3 统计范围：判定不出 → unbound；两处判出不同 → conflicted。"""
    span = _first_span(assertion, spans_by_id)
    ctx = getattr(span, "bound_context", None) or {}
    ctx_scope = (_classify_scope(ctx.get("scope"))
                 or _classify_scope(ctx.get("row_label")))
    txt_scope = _classify_scope(assertion.text)

    vals = {s for s in (ctx_scope, txt_scope) if s}
    if not vals:
        return _Chk("B3", False, "unbound",
                    "无法判定统计范围（row_label 与 text 均未给出单一方案/本报告期合计/多年累计）")
    if len(vals) > 1:
        return _Chk("B3", False, "conflict",
                    f"统计范围冲突：正文={txt_scope or '—'}，行标={ctx_scope or '—'}")
    scope = next(iter(vals))
    return _Chk("B3", True, "", f"统计范围判定为「{scope}」")


def _check_b4(assertion: Assertion, spans_by_id: dict) -> _Chk:
    """B4 口径：bound_context / text / assertion.caliber 三者不得互相冲突。"""
    span = _first_span(assertion, spans_by_id)
    ctx = getattr(span, "bound_context", None) or {}
    ctx_caliber = _classify_caliber(ctx.get("caliber")) or _classify_caliber(ctx.get("row_label"))
    txt_caliber = _classify_caliber(assertion.text)
    asr_caliber = _classify_caliber(assertion.caliber)

    vals = {c for c in (ctx_caliber, txt_caliber, asr_caliber) if c}
    if len(vals) > 1:
        return _Chk("B4", False, "conflict",
                    f"口径冲突：正文={txt_caliber or '—'}，行标={ctx_caliber or '—'}，"
                    f"断言={asr_caliber or '—'}")
    if not vals:
        return _Chk("B4", True, "", "未标明口径，无冲突")
    return _Chk("B4", True, "", f"口径一致：{next(iter(vals))}")


# ---------------------------------------------------------------------------
# 聚合与权限不对称
# ---------------------------------------------------------------------------


def _aggregate(chks: list[_Chk]) -> str:
    """按 未命中 > 冲突 > 未判定 > 绑定 的优先级归总。"""
    if not chks[0].ok and chks[0].kind == "unbound":
        # B1 连概念都解析不出，后面几项无从谈起。
        return SemanticStatus.UNBOUND.value
    kinds = {c.kind for c in chks if not c.ok}
    if "conflict" in kinds:
        return SemanticStatus.CONFLICTED.value
    if "unbound" in kinds:
        return SemanticStatus.UNBOUND.value
    return SemanticStatus.BOUND.value


def _apply_llm_verdict(status: str, llm_verdict: str | None) -> tuple[str, dict | None]:
    """权限不对称：llm_verdict 只能把 bound 拉低成 conflicted，不能反向抬升。"""
    if llm_verdict is None:
        return status, None
    token = str(llm_verdict).strip().lower()

    if token in _LLM_LOWER:
        # 只有 bound 允许被拉低；unbound / conflicted 原样保留（不合法侧向移动也拒绝）。
        if status == SemanticStatus.BOUND.value:
            return SemanticStatus.CONFLICTED.value, {
                "code": "LLM", "ok": True,
                "why": "llm_verdict=conflicted：按权限不对称，把 bound 拉低为 conflicted"}
        return status, {
            "code": "LLM", "ok": True,
            "why": f"llm_verdict=conflicted 但规则判定已是 {status}，只降不升，维持现状"}

    if token in _LLM_RAISE:
        # 关键不等号：批准权在规则与知识库，LLM 不能把 unbound / conflicted 抬成 bound。
        if status == SemanticStatus.BOUND.value:
            return status, {
                "code": "LLM", "ok": True,
                "why": "llm_verdict=bound：规则判定本已 bound，维持 bound"}
        return status, {
            "code": "LLM", "ok": True,
            "why": f"llm_verdict=bound 被拒：不得把规则判定的 {status} 抬成 bound（权限不对称）"}

    return status, {
        "code": "LLM", "ok": True,
        "why": f"llm_verdict={llm_verdict!r} 无法识别，忽略"}


# ---------------------------------------------------------------------------
# 公共入口
# ---------------------------------------------------------------------------


def bind(assertion: Assertion, *, spans: list, kb, fact_store=None,
         llm_verdict: str | None = None) -> BindingOutcome:
    """对一条断言做 B1~B4 语义绑定，返回 ``BindingOutcome``。

    参数：
        assertion: 待绑定的断言（本函数只改它的 ``semantic_status`` / ``metric_id`` /
            ``gate_trace``，**绝不改 ``evidence_status``**）。
        spans: 全部 Span 列表（用于取 ``bound_context.row_label`` 等）。
        kb: ``KnowledgeBase`` 实例（只读查询接口）。
        fact_store: 预留（本函数不写库；绑定结果是否落库由调用方决定）。
        llm_verdict: 模型意见；只能拉低，不能抬升。

    返回的 ``BindingOutcome.assertion`` 就是传入的那条断言（原地更新）。
    """
    spans_by_id = {getattr(s, "span_id", None): s for s in (spans or [])}
    _ = fact_store  # 预留参数：语义绑定本身不写事实库

    chks = [
        _check_b1(assertion, kb),
        _check_b2(assertion),
        _check_b3(assertion, spans_by_id),
        _check_b4(assertion, spans_by_id),
    ]
    status = _aggregate(chks)

    checks = [{"code": c.code, "ok": c.ok, "why": c.why} for c in chks]
    status, llm_check = _apply_llm_verdict(status, llm_verdict)
    if llm_check is not None:
        checks.append(llm_check)

    assertion.semantic_status = status
    trace = dict(assertion.gate_trace or {})
    trace["binding_checks"] = checks
    assertion.gate_trace = trace

    return BindingOutcome(assertion=assertion, checks=checks, semantic_status=status)


__all__ = ["BindingOutcome", "bind"]
