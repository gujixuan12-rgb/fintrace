"""规划器：组装决策上下文 + 调模型 + 校验动作（对应 BUILD_SPEC §8 · v2.2 §4.4）。

上下文六块（只送该送的，不送年报全文 / 历史工具输出 / 已 close 主张细节）：
1 固定头（任务目标 + 硬边界剩余额度 + 严格 JSON 契约）
2 待解问题（open_questions，驱动检索的键）
3 按需取回的原文片段（证据库里的 Span 原文，不是摘要）
4 按需查询的规则（知识库公式与适用条件）
5 已核验事实（事实库 only_facts=True 的相关断言摘要）
6 上一轮收据（state.steps 尾条）

时间基准：``historical_replay`` 下，``first_published_at > cutoff`` 的线索**不进上下文**，
并把本轮上下文里线索首曝时间的区间写进 ``context_leads_span``（供 decisions.jsonl 审计）。
"""
from __future__ import annotations

import datetime as _dt

from ..contracts import ACTIONS, Decision, digest

#: 上下文里最多放多少条原文片段（压 token，只放相关的一小撮）
MAX_SPANS = 12
#: 已核验事实最多放几条
MAX_FACTS = 20


def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def _lead_first_pub(lead) -> str:
    """取线索的首曝时间（Lead 对象或 dict 都认）。"""
    if isinstance(lead, dict):
        return str(lead.get("first_published_at") or "")
    return str(getattr(lead, "first_published_at", "") or "")


def _lead_to_dict(lead) -> dict:
    if isinstance(lead, dict):
        return {k: lead.get(k) for k in ("lead_id", "url", "title", "first_published_at")}
    return {"lead_id": getattr(lead, "lead_id", ""), "url": getattr(lead, "url", ""),
            "title": getattr(lead, "title", ""),
            "first_published_at": getattr(lead, "first_published_at", "")}


def filter_leads_by_as_of(leads, *, as_of_mode: str, cutoff: str) -> tuple[list[dict], tuple[str, str]]:
    """按时间基准过滤线索，返回 ``(进上下文的线索列表, 首曝时间区间)``。

    ``historical_replay``：只留 ``first_published_at`` 非空且 ``<= cutoff`` 的线索；
    无首曝时间的线索一律判为不可用（与 store.snapshots.before_cutoff 同口径）。
    ``post_hoc_review``：全部线索保留。
    """
    kept: list[dict] = []
    pubs: list[str] = []
    for lead in leads or []:
        pub = _lead_first_pub(lead)
        if as_of_mode == "historical_replay":
            if not (pub and cutoff) or pub > cutoff:
                continue
        kept.append(_lead_to_dict(lead))
        if pub:
            pubs.append(pub)
    span = (min(pubs), max(pubs)) if pubs else ("", "")
    return kept, span


def _purpose(name: str) -> str:
    """工具用途：取工具模块 docstring 的第一行（不另抄一份说明，避免两处定义）。"""
    import sys

    mod = sys.modules.get(f"fintrace_agent.tools.{name}")
    doc = (getattr(mod, "__doc__", "") or "").strip()
    if not doc:
        return ""
    first = doc.splitlines()[0].strip()
    return first.split("——", 1)[-1].strip() or first


def tool_manual(ctx=None) -> list[dict]:
    """工具手册：动作名 + 用途 + 参数契约 + 是否可用。

    **为什么必须在上下文里**：真实跑过一轮才发现，模型既不知道有哪些工具，
    也不知道每个工具要什么参数，于是把「官方检索」当成搜索框，反复猜参数、
    反复撞 ``bad_args``，把步数全烧光，一次本地材料都没解析。
    """
    from ..tools import registry

    try:
        avail = registry.configured_map(ctx)
    except Exception:                                            # noqa: BLE001
        avail = {}

    out: list[dict] = []
    for spec in registry.all_specs():
        params = spec.params or {}
        usable = bool(spec.enabled)
        note = ""
        if not spec.enabled:
            note = "未启用，不要选"
        elif spec.reserved:
            usable = bool(avail.get(spec.name, False))
            if not usable:
                note = "预留接口、当前未配置：选了只会拿到失败，别浪费一步"
        out.append({
            "action": spec.name,
            "purpose": _purpose(spec.name),
            "required_args": list(params.get("required") or []),
            "args": sorted((params.get("properties") or {}).keys()),
            "available": usable,
            "note": note,
        })
    return out


def build_context(state, *, evidence, kb, fact_store, as_of_mode: str, leads=None,
                  documents=None, tools=None) -> dict:
    """组装本轮决策上下文。**确定性**：同 state + 同输入 → 同上下文。

    ``evidence`` 是证据日志（有 ``spans()`` / ``assertions()``）；``kb`` 是知识库；
    ``fact_store`` 是事实库。任一为 None 时对应块留空（降级不崩）。
    ``leads`` 是待过滤的联网线索（可选；不在 spec 主签名里，作为额外注入点）。

    新增两块（2026-10-10，真模型跑完才发现缺它们）：
    ``documents`` 本地材料清单（模型最需要的那一块：材料已在手）；
    ``tools`` 工具手册（不传则本函数按注册表现算）。
    """
    cutoff = getattr(state.bounds, "cutoff_date", "") or ""
    counting = state.counting or {}
    remaining_tools = max(0, int(state.bounds.max_tool_calls) - int(counting.get("tool_calls", 0)))
    remaining_tokens = max(0, int(state.bounds.token_budget) - int(counting.get("tokens", 0)))

    # 块 1：固定头
    header = {
        "task": state.goal.get("task", "audit_draft"),
        "case_id": state.goal.get("case_id", ""),
        "company": state.goal.get("company", ""),
        "period": state.goal.get("period", ""),
        "as_of": state.as_of,
        "as_of_mode": as_of_mode,
        "remaining_tool_calls": remaining_tools,
        "remaining_tokens": remaining_tokens,
    }
    output_contract = ('{"action": <动作名>, "args": {...}, "why": <一句话理由>} —— '
                       '严格 JSON，无多余文字；动作必须在动作白名单内。')

    # 块 2：待解问题
    open_questions: list[str] = []
    for claim in state.open_claims:
        for q in claim.open_questions:
            if q:
                open_questions.append(f"[{claim.claim_id}] {q}")

    # 块 3：按需取回的原文片段（Span 原文，不是摘要）
    spans_out: list[dict] = []
    if evidence is not None:
        try:
            all_spans = evidence.spans()
        except Exception:
            all_spans = []
        for sp in all_spans[:MAX_SPANS]:
            spans_out.append({"span_id": getattr(sp, "span_id", ""),
                              "page": getattr(sp, "page", None),
                              "text": getattr(sp, "quoted_text", "")})

    # 块 4：按需查询的规则（命中指标的公式）
    rules_out: list[dict] = []
    if kb is not None:
        seen_metrics: set[str] = set()
        for claim in state.open_claims:
            key = claim.metric_key
            if not key or key in seen_metrics:
                continue
            seen_metrics.add(key)
            try:
                concept = kb.resolve_metric(key)
            except Exception:
                concept = None
            if not concept:
                continue
            mid = concept.get("metric_id", "")
            try:
                formula = kb.formula(mid)
            except Exception:
                formula = None
            rules_out.append({"metric": key, "metric_id": mid,
                              "formula": (formula or {}).get("expression", ""),
                              "applicable_when": (formula or {}).get("applicable_when", {})})

    # 块 5：已核验事实
    facts_out: list[dict] = []
    if fact_store is not None:
        try:
            rows = fact_store.query(subject=state.goal.get("company", ""),
                                    period=state.goal.get("period", ""), only_facts=True)
        except Exception:
            rows = []
        for a in rows[:MAX_FACTS]:
            facts_out.append({"assertion_id": getattr(a, "assertion_id", ""),
                              "subject": getattr(a, "subject", ""),
                              "period": getattr(a, "period", ""),
                              "metric": getattr(a, "metric", ""),
                              "value": getattr(a, "value", ""),
                              "unit": getattr(a, "unit", "")})

    # 块 5.5：已登记了什么（一眼可见，避免模型「忘了自己已经解析过」）
    n_spans = n_assert = 0
    parsed_docs: list = []
    try:
        if evidence is not None:
            got = list(evidence.spans())
            n_spans = len(got)
            n_assert = len(list(evidence.assertions()))
            parsed_docs = sorted({str(getattr(s, "doc_id", "") or "") for s in got} - {""})
    except Exception:                                            # noqa: BLE001
        pass
    header["registered"] = {"spans": n_spans, "assertions": n_assert,
                            "parsed_docs": parsed_docs}

    # 块 6：工具手册（哪些动作能用、要什么参数）
    tools_out = list(tools) if tools is not None else tool_manual()

    # 块 7：本地材料清单 —— 模型最缺的一块
    docs_out: list[dict] = []
    for d in documents or []:
        g = (d.get if isinstance(d, dict) else lambda k, dv=None: getattr(d, k, dv))
        pub = str(g("publication_date", "") or "")
        usable, note = True, ""
        if cutoff:
            if not pub:
                usable, note = False, "无公开日期，时间硬门判为不可用"
            elif pub > cutoff:
                usable, note = False, f"公开日 {pub} 晚于截止日 {cutoff}，已排除，不得引用"
        docs_out.append({
            "doc_id": str(g("doc_id", "") or ""),
            "file_name": str(g("file_name", "") or ""),
            "path": str(g("content_path", "") or ""),          # 可直接作为 t_parse 的路径参数
            "format": str(g("material_format", "") or ""),
            "publication_date": pub,
            "page_count": g("page_count", None),
            "usable": usable,
            "note": note,
        })

    # 块 8：硬提示（短句、可执行；说的是边界，不是让模型「加油」）
    guidance = [
        "本地材料已经在手（见 documents）：先用 t_parse / t_extract 解析它们，"
        "不要上网去找已经在手的年报。",
        "documents 里 usable=false 的材料一律不得引用（晚于截止日或没有公开日期）。",
        "tools 里 available=false 的动作不要选，选了只会拿到失败收据。",
        "每条结论都要能回到「文件 + 页码 + 原文片段」；定位不到就报 insufficient，不许编页码。",
        "同一页不要反复取：先用 t_lookup / t_recall 看已经登记过的证据。",
        "header.registered 是你已经拿到的东西：parsed_docs 里的材料**已经解析过，不要重复解析**；"
        "解析完之后该做的是 t_extract（抽候选断言）或 t_compute（复算）。",
        "不要一次解析整本年报：t_parse 用 pages 参数按页范围分批取，"
        "先看草稿里主张涉及哪些科目，再定向取那几页。",
    ]

    # 块 9：上一轮收据（含失败原因与正确参数 —— 让模型能自己纠正）
    last_receipt: dict = {}
    if state.steps:
        last = dict(state.steps[-1])
        last_receipt = {
            "action": last.get("action", ""),
            "ok": last.get("ok"),
            "error": last.get("error", ""),
            "message": str(last.get("message") or "")[:400],
            "args": last.get("args") or {},
            "progress": last.get("progress", ""),
        }
        if last.get("error") in ("bad_args", "unknown_tool", "disabled", "no_handler",
                                 "precondition", "tool_permission"):
            for t in tools_out:
                if t.get("action") == last.get("action"):
                    last_receipt["hint"] = (
                        f'动作 {last.get("action")} 的可用参数键：{t.get("args")}；'
                        f'必填：{t.get("required_args")}；用途：{t.get("purpose")}')
                    break

    # 线索：按 as_of 过滤
    kept_leads, leads_span = filter_leads_by_as_of(
        leads, as_of_mode=as_of_mode, cutoff=cutoff)

    return {
        "header": header,
        "open_questions": open_questions,
        "documents": docs_out,
        "tools": tools_out,
        "guidance": guidance,
        "spans": spans_out,
        "rules": rules_out,
        "facts": facts_out,
        "last_receipt": last_receipt,
        "leads": kept_leads,
        "context_leads_span": leads_span,
        "as_of": state.as_of,
        "output_contract": output_contract,
    }


def decide(state, client, *, context: dict, prompt_version: str = "p1") -> Decision:
    """调模型拿一次决策，解析并校验；非法决策以 ``valid=False`` 返回（不抛）。

    返回的 ``Decision`` 额外挂 ``usage`` 属性（dict），供回路累加 token；不算契约字段。
    """
    model_name = str(getattr(client, "model", "") or "")
    base = dict(run_id=state.run_id, n=0, prompt_version=prompt_version,
                model=model_name, state_digest=digest(state.to_dict()),
                as_of=state.as_of,
                context_leads_span=tuple(context.get("context_leads_span", ("", ""))),
                at=_now())

    try:
        raw, usage = client.decide(context)
    except Exception as e:                                   # 未配置 / 网络 / 其它
        d = Decision(action="", why="", raw_response="", valid=False,
                     invalid_reason=f"{type(e).__name__}: {e}", **base)
        d.usage = {}
        return d

    base["raw_response"] = raw or ""
    try:
        parsed = _parse(raw)
    except Exception as e:
        d = Decision(action="", why="", valid=False, invalid_reason=str(e), **base)
        d.usage = usage or {}
        return d

    action = parsed["action"]
    if action not in ACTIONS:
        d = Decision(action=action, args=parsed["args"], why=parsed["why"], valid=False,
                     invalid_reason=f"动作 {action!r} 不在动作白名单内", **base)
        d.usage = usage or {}
        return d

    d = Decision(action=action, args=parsed["args"], why=parsed["why"], valid=True, **base)
    d.usage = usage or {}
    return d


def _parse(raw: str) -> dict:
    from .model import parse_decision
    return parse_decision(raw)


__all__ = ["build_context", "decide", "filter_leads_by_as_of", "tool_manual",
           "MAX_SPANS", "MAX_FACTS"]
