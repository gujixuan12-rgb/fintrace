"""端到端编排：一次核查从案件到报告（v2.2 §4.3）。

两种模式：
* ``fixed``：直接走搬来的确定性内核（基线，口径与团队仓库一致；也是没有模型时的回落）。
* ``agent``：走自主回路；材料侧的登记与比对仍由确定性代码完成（判定权不在模型手里）。

边界说明（诚实写在代码里）：agent 模式下，**调查路径**由模型定（调哪些工具、取哪些页），
**判定**由本模块的确定性函数做（比对、修正值、引用绑定），**发布**由 V 门裁决。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Optional
from pathlib import Path
from types import SimpleNamespace

from fintrace_agent import paths as P
from fintrace_agent.cases.adapter import (
    CaseInput,
    documents_of,
    draft_to_claims,
    find_case,
    read_draft,
)
from fintrace_agent.cases.fixed_runner import page_texts_reader, run_fixed
from fintrace_agent.contracts import (
    ApplicabilityField,
    Assertion,
    ExtractorTier,
    Span,
    SpanKind,
    Claim,
    ClaimStatus,
    Finding,
    MatchResult,
    Origin,
    Publish,
    Relation,
    RelationPredicate,
    TaskState,
    Verdict,
    digest,
)
from fintrace_agent.gate.finding_gate import FindingGate
from fintrace_agent.store.runs import atomic_write_json
from fintrace_agent.reporting.report import findings_payload, write_outputs
from fintrace_agent.store import blobs
from fintrace_agent.store.evidence import EvidenceLog
from fintrace_agent.store.runs import new_run_id, run_dir


@dataclass
class Outcome:
    run_id: str
    run_dir: str
    mode: str
    exit: str
    state: TaskState
    findings: list[Finding] = field(default_factory=list)
    gate: object | None = None
    payload: dict = field(default_factory=dict)
    outputs: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def counts(self) -> dict:
        c: dict[str, int] = {}
        for f in self.findings:
            c[f.verdict] = c.get(f.verdict, 0) + 1
        return c

    def to_dict(self) -> dict:
        return {"run_id": self.run_id, "run_dir": self.run_dir, "mode": self.mode,
                "exit": self.exit, "counts": self.counts(),
                "gate_counts": (dict(getattr(self.gate, "counts", {}) or {})),
                "notes": list(self.notes)}


# ---------------------------------------------------------------------------
# 案件 → 状态
# ---------------------------------------------------------------------------


def _state_for(case: CaseInput, *, run_id: str, mode: str, claims: list[Claim]) -> TaskState:
    st = TaskState(run_id=run_id, mode=mode)
    st.goal = {"task": "audit_draft", "case_id": case.case_id, "company": case.company,
               "period": case.period}
    st.artifact_fingerprint = digest(st.goal)
    st.bounds.cutoff_date = case.prediction_cutoff_date
    st.bounds.as_of_mode = "historical_replay"
    st.as_of = case.prediction_cutoff_date
    st.claims = list(claims)
    return st


def _write_decisions_snapshot(run_id: str, rows: list[dict]) -> None:
    p = run_dir(run_id) / "decisions.jsonl"
    with p.open("a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def _rejections_path(run_id: str) -> Path:
    return run_dir(run_id) / "rejections.jsonl"


def _log_rejections(run_id: str, rejected: list[dict]) -> None:
    if not rejected:
        return
    with _rejections_path(run_id).open("a", encoding="utf-8") as fh:
        for r in rejected:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# fixed 模式
# ---------------------------------------------------------------------------


_STATUS_BY_VERDICT = {
    Verdict.SUPPORTED.value: ClaimStatus.VERIFIED.value,
    Verdict.CONTRADICTED.value: ClaimStatus.VERIFIED.value,
    Verdict.INSUFFICIENT.value: ClaimStatus.INSUFFICIENT.value,
    Verdict.PREDICTION.value: ClaimStatus.BLOCKED.value,
}


def _claims_from_fixed_rows(rows: list[dict]) -> list[Claim]:
    out: list[Claim] = []
    for r in rows:
        v = (r.get("verdict") or {})
        label = str(v.get("label") or Verdict.INSUFFICIENT.value)
        out.append(Claim(
            claim_id=str(r.get("claim_id") or ""),
            claim_text=str(r.get("source_sentence") or r.get("assertion") or ""),
            subject="", period=str(r.get("claim_period") or ""),
            metric_key=str(r.get("metric_key") or ""),
            claim_value=(None if r.get("claim_value") in (None, "") else str(r.get("claim_value"))),
            claim_unit=str(r.get("claim_unit") or ""),
            status=_STATUS_BY_VERDICT.get(label, ClaimStatus.INSUFFICIENT.value),
            blocked_reason=(str(v.get("unable_reason") or "") or None),
        ))
    return out


def run_fixed_case(case_id: str, *, root: Path | None = None, use_llm: bool = False,
                   run_id: str = "") -> Outcome:
    case = find_case(case_id, root=root)
    docs = documents_of(case, root=root)
    fixed = run_fixed(case, root=root, use_llm=use_llm)
    rid = run_id or new_run_id(case.case_id)

    state = _state_for(case, run_id=rid, mode="fixed",
                       claims=_claims_from_fixed_rows(fixed.raw.claims))
    state.steps.append({"action": "fixed_pipeline", "ok": True,
                        "run_log_id": fixed.run_log_id, "documents": [d.file_name for d in docs]})

    gate = FindingGate(evidence=None, kb=None, state=state,
                       page_texts=page_texts_reader(case, root=root),
                       assertions_by_id={}, require_refs=False)
    gate_res = gate.review(fixed.findings)

    payload = findings_payload(gate_res.findings, state, gate=gate_res, mode="fixed",
                               extra={"engine_run_log_id": fixed.run_log_id,
                                      "documents": [d.to_dict() for d in docs],
                                      "warnings": fixed.warnings})
    out_dir = run_dir(rid, create=True)
    outputs = write_outputs(out_dir, payload)
    atomic_write_json(out_dir / "state.json", state.to_dict())
    _write_decisions_snapshot(rid, [{"n": 1, "action": "fixed_pipeline", "valid": True,
                                     "why": "固定链路（基线）：不走模型决策",
                                     "bound_decision": "allow", "state_digest": digest(state.to_dict()),
                                     "as_of": state.as_of, "model": "", "prompt_version": ""}])
    return Outcome(run_id=rid, run_dir=str(out_dir), mode="fixed", exit="fixed",
                   state=state, findings=list(gate_res.findings), gate=gate_res,
                   payload=payload, outputs=outputs)


# ---------------------------------------------------------------------------
# agent 模式
# ---------------------------------------------------------------------------


def _decimal(text) -> Decimal | None:
    if text in (None, ""):
        return None
    try:
        return Decimal(str(text).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None


def _applicability(claim: Claim, fact: Assertion, *, kb=None) -> dict:
    """适用条件逐字段填 (主张值, 证据值, 匹配结论) —— 匹配结论由程序算，模型填的一律忽略。"""
    def mk(x, y, *, note=""):
        same = (str(x) == str(y)) and x not in (None, "")
        return ApplicabilityField(claim=x, evidence=y, note=note,
                                  match=(MatchResult.VERIFIED.value if same
                                         else MatchResult.UNKNOWN.value))
    metric_id = fact.metric_id or fact.metric
    if kb is not None:
        resolved = kb.resolve_metric(claim.metric_key or claim.claim_text)
        if resolved and resolved.get("metric_id"):
            metric_id = resolved["metric_id"]
    # 比的是**归一后的规范值**：草稿写 metric_key=revenue、期间 2023A，
    # 材料里写「营业收入」、期间「2023年」—— 拿显示名比会把同一条判成「不适用」。
    fields: dict = {}

    def put(name: str, x, y, *, note: str = "") -> None:
        """只在至少一侧有值时才作为适用条件。

        双方都为空的字段（比如口径）不该参与判定：空对空会被算成「不匹配」，
        结果所有关系都判成不适用、全部主张退化成信息不足（实测踩过）。
        """
        if not str(x or "").strip() and not str(y or "").strip():
            return
        fields[name] = mk(x, y, note=note)

    put("period", _norm_period(claim.period), _norm_period(fact.period),
        note="期间按年份归一后比较")
    put("metric", str(metric_id or "").strip().lower(),
        str(fact.metric_id or fact.metric or "").strip().lower(),
        note="指标按归一 id 比较")
    if _norm_unit(claim.claim_unit) or _norm_unit(fact.unit):
        fields["unit"] = ApplicabilityField(
            claim=_norm_unit(claim.claim_unit), evidence=_norm_unit(fact.unit),
            note="按基本单位比（亿元↔元 算同一族，换算在比数值时做）",
            match=(MatchResult.VERIFIED.value
                   if _units_comparable(claim.claim_unit, fact.unit)
                   else MatchResult.UNKNOWN.value))
    put("caliber", "", fact.caliber, note="材料标了统计口径而主张未标")
    return fields


def _row_span(run_id: str, doc_id: str, page, row: dict, idx: int) -> Span:
    """把表格行做成 ``table_row`` 切片，并填好语义轴需要的上下文。

    这些信息是**程序从版式里读出来的**，不是猜的：行标=科目名，单位=括号里的单位，
    期间=列头（2023年/2023年末），统计范围=本报告期（该列就是本期）。
    行文里已经能判出范围时不再补 scope，避免制造「范围冲突」。
    """
    text = str(row.get("quoted_text") or row.get("claim_text") or "")
    ctx = {
        "row_label": str(row.get("metric") or ""),
        "unit": str(row.get("unit") or ""),
        "period": str(row.get("period_hint") or row.get("period") or ""),
        "scope": "",
    }
    try:
        from fintrace_agent.tools.semantic_binding import _classify_scope

        if not _classify_scope(text):
            ctx["scope"] = "本报告期"
    except Exception:                                            # noqa: BLE001
        pass
    return Span(
        span_id=f"{run_id}-ROW-{doc_id}-{page}-{idx}",
        doc_id=str(doc_id), page=page,
        char_range=(0, 0), quoted_text=text,
        span_kind=SpanKind.TABLE_ROW.value, bound_context=ctx,
        extractor=ExtractorTier.BUILTIN.value)


#: 单位换算表（都以「元 / 股 / %」为基本单位）
_UNIT_SCALE = {
    "元": Decimal(1), "千元": Decimal("1E+3"), "万元": Decimal("1E+4"),
    "十万元": Decimal("1E+5"), "百万元": Decimal("1E+6"), "亿元": Decimal("1E+8"),
    "股": Decimal(1), "万股": Decimal("1E+4"), "亿股": Decimal("1E+8"),
    "%": Decimal(1), "个百分点": Decimal(1), "倍": Decimal(1),
    "元/股": Decimal(1), "元/10股": Decimal(1),
}


def _norm_unit(u) -> str:
    return str(u or "").replace(" ", "").replace("（", "").replace("）", "").strip()


def _base_unit(u) -> str:
    n = _norm_unit(u)
    return {"千元": "元", "万元": "元", "十万元": "元", "百万元": "元", "亿元": "元",
            "万股": "股", "亿股": "股"}.get(n, n)


def _units_comparable(a, b) -> bool:
    """两个单位能不能比。草稿常把每股指标的单位写成「元」，这一对按可比处理。"""
    ba, bb = _base_unit(a), _base_unit(b)
    if not ba or not bb:
        return False
    if ba == bb:
        return True
    return {ba, bb} == {"元", "元/股"}


def _convert(value: Decimal, frm: str, to: str) -> Optional[Decimal]:
    """把 ``value`` 从 ``frm`` 单位换算到 ``to`` 单位；不可比返回 None。"""
    nf, nt = _norm_unit(frm), _norm_unit(to)
    if nf == nt:
        return value
    if _base_unit(nf) != _base_unit(nt):
        # 「元/股」与「元」按 1:1 处理：草稿常把每股指标的单位写成「元」，
        # 而材料写「元/股」。不这样处理，每股收益这类差 10 倍的错误就判不出来
        # （只能退化成「单位不可比、信息不足」）。
        if {_base_unit(nf), _base_unit(nt)} == {"元", "元/股"}:
            return value
        return None
    sf = _UNIT_SCALE.get(nf, Decimal(1))
    st = _UNIT_SCALE.get(nt, Decimal(1))
    if st == 0:
        return None
    return value * sf / st


def _decimals_of_text(text) -> Optional[int]:
    """主张披露了几位小数（用于按主张精度对齐比较）。"""
    s = str(text or "")
    if "." not in s:
        return None
    frac = "".join(ch for ch in s.split(".", 1)[1] if ch.isdigit())
    return len(frac) if frac else None


def _norm_period(text) -> str:
    """期间归一成 ``YYYY``：材料写「2023年」「2023年度」、草稿写「2023A」，都归到 ``2023``。"""
    m = re.search(r"(\d{4})", str(text or ""))
    return m.group(1) if m else ""


def _resolve_claims(state: TaskState, *, facts, kb, run_id: str) -> list[Finding]:
    """用事实库里已核验的断言去判定草稿主张（确定性代码做判定，模型不参与）。"""
    findings: list[Finding] = []
    company = state.goal.get("company", "")
    goal_period = state.goal.get("period", "")
    # 事实库一次取全，自己按「归一后的期间 + 指标」筛。
    # 为什么不能直接按字符串查：库里存的是材料里的「2023年」，草稿写的是「2023A」，
    # 直接查一条都命不中（实测：18 条断言已入库，15 条主张仍然全部「信息不足」）。
    pool = []
    if facts is not None:
        try:
            pool = list(facts.query(subject=company, period="", metric="", only_facts=True))
        except Exception:                                        # noqa: BLE001
            pool = []

    for i, claim in enumerate(list(state.claims)):
        want_p = _norm_period(claim.period or goal_period)
        same_period = [a for a in pool if _norm_period(getattr(a, "period", "")) == want_p]
        key = str(claim.metric_key or "").strip().lower()
        cands = [a for a in same_period
                 if key and key == str(getattr(a, "metric_id", "") or "").strip().lower()]
        if not cands:                       # 指标 id 对不上就退一步比指标名
            cands = [a for a in same_period
                     if key and str(getattr(a, "metric", "") or "").strip() == claim.metric_key]
        if not cands:
            cands = [a for a in same_period
                     if key and key in str(getattr(a, "metric_id", "") or "").lower()]

        if not cands:
            claim.status = ClaimStatus.INSUFFICIENT.value
            have = "、".join(sorted({str(getattr(a, "metric", "")) for a in same_period})) or "无"
            claim.blocked_reason = (
                f"材料中未找到同指标的已核验断言（期间 {want_p or '未标'}；"
                f"该期间库里已有：{have}）")
            # 未解决也要留下一条结论 —— 只在报告里写「解决了的那部分」等于隐瞒。
            findings.append(Finding(
                finding_id=f"F-{claim.claim_id}", claim_id=claim.claim_id,
                verdict=Verdict.INSUFFICIENT.value, basis=claim.blocked_reason,
                publish=Publish.MANUAL_REVIEW.value))
            continue

        fact = cands[0]
        claim.evidence_ids = [fact.assertion_id]
        rel = Relation(
            relation_id=f"R-{claim.claim_id}", predicate=RelationPredicate.EQUALS.value,
            subject_ref=claim.claim_id, object_ref=fact.assertion_id,
            applicability=_applicability(claim, fact, kb=kb),
            rule_id="draft_vs_material", rule_version="v1", status="candidate",
            run_id=run_id,
        )
        a_val, b_val = _decimal(claim.claim_value), _decimal(fact.value)
        places = _decimals_of_text(claim.claim_value)
        basis_extra = ""
        if a_val is None or b_val is None or not rel.applicable:
            claim.status = ClaimStatus.INSUFFICIENT.value
            claim.blocked_reason = ("数值或适用条件不足以判定"
                                    + (f"（不成立字段：{','.join(rel.failed_fields)}）"
                                       if rel.failed_fields else ""))
            verdict = Verdict.INSUFFICIENT.value
            correction: dict = {}
        elif not _units_comparable(claim.claim_unit, fact.unit):
            claim.status = ClaimStatus.INSUFFICIENT.value
            claim.blocked_reason = (f"单位不可比：主张 {claim.claim_unit or '未标'!r} "
                                    f"vs 材料 {fact.unit or '未标'!r}")
            verdict = Verdict.INSUFFICIENT.value
            correction = {}
        else:
            # 换算到主张的单位，并按**主张披露的精度**对齐后再比：
            # 主张写「2,931.56 万元」时，材料全精度 29,315,603.26 元 ≈ 2,931.560326 万元，
            # 不按主张精度对齐就会把一条正确的主张判成错（精度事故的反面）。
            b_cmp = _convert(b_val, fact.unit, claim.claim_unit)
            if places is not None and b_cmp is not None:
                b_cmp = b_cmp.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
                basis_extra = f"（材料值已换算到 {claim.claim_unit or '同单位'} 并按 {places} 位小数对齐）"
            if b_cmp is None:
                claim.status = ClaimStatus.INSUFFICIENT.value
                claim.blocked_reason = f"单位换算不了：{fact.unit!r} → {claim.claim_unit!r}"
                verdict = Verdict.INSUFFICIENT.value
                correction = {}
            elif a_val == b_cmp:
                claim.status = ClaimStatus.VERIFIED.value
                verdict = Verdict.SUPPORTED.value
                correction = {}
            else:
                claim.status = ClaimStatus.VERIFIED.value
                verdict = Verdict.CONTRADICTED.value
                correction = {"value": fact.value, "unit": fact.unit,
                              "source_page": (fact.source_spans or [None])[0],
                              "converted": str(b_cmp),
                              "converted_unit": claim.claim_unit}

        pages = []
        ev = []
        for sp in (fact.source_spans or []):
            pages.append(None)
            ev.append({"assertion_id": fact.assertion_id, "span_id": sp,
                       "value": fact.value, "raw": fact.raw_text, "unit": fact.unit})
        f = Finding(finding_id=f"F-{claim.claim_id}", claim_id=claim.claim_id,
                    verdict=verdict,
                    basis=f"与已核验断言 {fact.assertion_id} 比对{basis_extra}",
                    correction=correction, evidence=ev,
                    assertion_refs=[fact.assertion_id], relation_refs=[rel.relation_id])
        findings.append(f)
    return findings


def run_agent_case(case_id: str, *, root: Path | None = None, client=None,
                   max_steps: int | None = None, run_id: str = "",
                   db_paths: dict | None = None) -> Outcome:
    from fintrace_agent.agent import loop as agent_loop
    from fintrace_agent.facts.store import FactStore
    from fintrace_agent.knowledge.loader import KnowledgeBase
    from fintrace_agent.tools import registry
    from fintrace_agent.tools.semantic_binding import bind
    from fintrace_agent.tools.validator import Validator

    case = find_case(case_id, root=root)
    docs = documents_of(case, root=root)
    rid = run_id or new_run_id(case.case_id)
    claims = draft_to_claims(read_draft(case, root=root), case=case)

    db = dict(db_paths or {})
    kb = KnowledgeBase(db.get("kb") or P.KNOWLEDGE_DB)
    facts = FactStore(db.get("facts") or P.FACTS_DB)
    evidence = EvidenceLog(rid)
    read = page_texts_reader(case, root=root)
    doc_by_name = {d.file_name: d for d in docs}
    validator = Validator(kb=kb, cutoff_date=case.prediction_cutoff_date, page_texts=read)

    def execute(name: str, args: dict, ctx):
        """工具执行 + W 门：任何材料侧抽取都必须过门才能登记。"""
        receipt = registry.execute(name, args, ctx)
        if not receipt.ok:
            return receipt
        result = receipt.result or {}

        def _unwrap(key: str) -> list:
            """取结果里的列表。超长列表会被注册表**卸载到内容寻址库**，
            此时取到的是 ``{"blob_ref","chars","type"}`` —— 要从 blob 读回，
            否则会以为「解析出了 0 条 span」（第一次跑就是这么被骗过去的）。
            """
            v = result.get(key)
            if isinstance(v, dict) and v.get("blob_ref"):
                try:
                    return json.loads(blobs.get_text(str(v["blob_ref"])))
                except Exception:                                # noqa: BLE001
                    return []
            return list(v or [])

        # 1) 解析类工具产出的是 **spans**（原文切片）—— 必须登记，否则
        #    state.evidence_index 一直为空，t_extract 的前置条件永远不满足，
        #    模型会把步数全烧在反复解析上（真模型跑过一轮才发现）。
        span_rows = _unwrap("spans")
        if span_rows:
            spans = []
            for row in span_rows:
                if not isinstance(row, dict):
                    continue
                d = dict(row)
                for k in ("char_range", "rect"):
                    if isinstance(d.get(k), list):
                        d[k] = tuple(d[k])
                try:
                    spans.append(Span.from_dict(d))
                except TypeError:
                    continue
            before = set(evidence.index())
            ids = evidence.add_spans(spans) if spans else []
            fresh = [i for i in ids if i not in before]
            if fresh:
                receipt.new_evidence = fresh
                receipt.progress = "new_evidence"
            elif ids:
                # 同一份材料被重复解析：**必须**说清楚「没有新增」。
                # 否则每步都像有进展，打转守门不触发，模型会一直重复解析
                # （真跑过：12 步里 7 次 t_parse，25731 token，一条主张没解决）。
                receipt.new_evidence = []
                receipt.progress = "none"
                receipt.result = dict(receipt.result or {})
                receipt.result["message"] = (
                    f"该材料此前已解析过：{len(ids)} 条原文片段已在证据库里，本次没有新增。"
                    f"不要重复解析，请改用 t_extract 抽取候选断言或 t_compute 复算。")

        if name in ("t_extract", "t_parse", "t_spec_extract"):
            rows = _unwrap("claims")
            if not rows:
                return receipt

            # 已登记的原文片段：候选断言必须**真的绑到它们身上**。
            # W4 要的是「数值与页码联合绑定」，校验器还要拿到被引用的 span 对象本身；
            # 早先这里传空 spans、source_spans 又是自己拼的假 id，
            # 于是每一条都被判「未绑定任何原文 span」（实测 48/48 全拒）。
            all_spans = list(evidence.spans())
            by_doc_page: dict = {}
            by_doc: dict = {}
            for s in all_spans:
                by_doc_page.setdefault((str(s.doc_id), s.page), []).append(s)
                by_doc.setdefault(str(s.doc_id), []).append(s)

            def _match(pool: list, quoted, value):
                q = "".join(str(quoted or "").split())
                v = str(value or "").replace(",", "")
                if q:
                    for s in pool:
                        t = "".join(str(getattr(s, "quoted_text", "") or "").split())
                        if q in t or t in q:
                            return s
                if v:
                    for s in pool:
                        if v in str(getattr(s, "quoted_text", "") or "").replace(",", ""):
                            return s
                return None

            def _pick(doc_id: str, page, quoted, value):
                """按「同材料同页 → 同材料任页 → 全库」三级找对应切片，以**内容一致**为准。

                为什么要有后两级：模型常常用 ``text`` 抽取（它把原文片段当参数递回来），
                这时 claims 里没有页码，只按「同页」找就永远匹配不上，于是所有候选都
                被判「找不到对应切片」（实测第 3~5 步连续三次如此）。绑定必须靠内容一致，
                页码是绑定成功后从切片上**读出来的**，不是用来筛的。
                """
                for pool in (by_doc_page.get((str(doc_id), page)) or [],
                             by_doc.get(str(doc_id)) or [],
                             all_spans):
                    hit = _match(pool, quoted, value)
                    if hit is not None:
                        return hit
                return None

            cands: list[Assertion] = []
            bound_spans: dict = {}
            meta_doc: dict = {}
            skipped = unbound = 0
            for j, r in enumerate(rows):
                if not isinstance(r, dict):
                    continue
                # 材料侧只登记**能归一到知识库指标**的数值断言。
                # 规则拆解跑在整份年报上会把「赛特广场5层」「不适用」这类也当候选，
                # W1 会照数拒掉 —— 门是在正确工作，但这些噪声压根不该生成。
                metric_name = str(r.get("metric") or "").strip()
                resolved = None
                if kb is not None and metric_name:
                    try:
                        resolved = kb.resolve_metric(metric_name)
                    except Exception:                            # noqa: BLE001
                        resolved = None
                metric_id = str((resolved or {}).get("metric_id") or "")
                if not metric_id:
                    skipped += 1
                    continue

                page = r.get("page")
                doc = doc_by_name.get(str(r.get("file_name") or "")) or docs[0]
                sp = _pick(doc.doc_id, page,
                           r.get("quoted_text") or r.get("claim_text"), r.get("value"))
                if sp is None:
                    unbound += 1          # 找不到对应切片就**不登记**，不硬凑绑定
                    continue

                doc = doc_by_name.get(str(r.get("file_name") or "")) \
                    or next((d for d in docs if d.doc_id == sp.doc_id), docs[0])

                # 行级切片：语义轴（B3 统计范围 / B4 口径）要看 bound_context，
                # 而纯文本解析出来的段落切片里没有这些信息 —— 不补，B3 判不出范围，
                # 断言就永远停在 unbound，也就永远成不了「事实」，主张全判信息不足。
                row_span = _row_span(rid, doc.doc_id, sp.page, r, j)
                evidence.add_spans([row_span])
                bound_spans[row_span.span_id] = row_span
                sp = row_span

                a = Assertion(
                    assertion_id=f"{rid}-{sp.doc_id}-{sp.page}-{j}",
                    kind="financial_value",
                    subject=case.company, period=str(r.get("period") or case.period),
                    metric=metric_name, metric_id=metric_id,
                    value=str(r.get("value") or ""), unit=str(r.get("unit") or ""),
                    # raw_text 必须是**披露的那个数**（W6 靠它判披露精度：0.0718 是 4 位就不能变 2 位）；
                    # 整行文字放 text，别塞进 raw_text —— 塞进去 W6 会判「不是纯数字串」而整批拒收。
                    raw_text=str(r.get("value") or ""),
                    text=str(r.get("quoted_text") or r.get("claim_text") or ""),
                    origin=Origin.MATERIAL.value, source_spans=[sp.span_id],
                    extracted_by={"how": "rule", "prompt_version": "p1"},
                    run_id=rid)
                cands.append(a)
                bound_spans[sp.span_id] = sp
                meta_doc[a.assertion_id] = doc

            if not cands:
                receipt.result = dict(receipt.result or {})
                receipt.result["message"] = (
                    f"本次候选 {len(rows)} 条都没有登记：指标无法归一 {skipped} 条、"
                    f"在已登记原文里找不到对应切片 {unbound} 条。"
                    f"请只取可归一的财务科目（营业收入 / 净利润 / 基本每股收益 / 总资产），"
                    f"并确认这些页已经 t_parse 过。")
                receipt.progress = "none"

            if cands:
                spans_list = list(bound_spans.values())
                adm = validator.admit(cands, spans=spans_list,
                                      doc=meta_doc.get(cands[0].assertion_id) or docs[0])
                kept = []
                for a in adm.accepted:
                    bo = bind(a, spans=spans_list, kb=kb, fact_store=facts)
                    facts.put_assertion(bo.assertion)
                    evidence.add_assertions([bo.assertion])
                    kept.append(bo.assertion.assertion_id)
                receipt.new_evidence = kept
                receipt.rejected = adm.rejected
                receipt.progress = ("new_evidence" if kept else
                                    ("evidence_rejected" if adm.rejected else "none"))
                if kept:
                    receipt.result = dict(receipt.result or {})
                    receipt.result["message"] = (
                        f"已登记 {len(kept)} 条已核验断言（W 门通过）；"
                        f"被拒 {len(adm.rejected)} 条。")
                _log_rejections(rid, adm.rejected)
        return receipt

    tools_ctx = {
        "execute": execute,
        "gate": lambda findings, state: FindingGate(
            evidence=evidence, kb=kb, state=state, page_texts=read,
            assertions_by_id={a.assertion_id: a for a in evidence.assertions()},
            require_refs=True).review(findings),
        "fixed_runner": lambda case_obj, state: run_fixed(case, root=root).raw,
        "kb": kb,
        "facts": facts,
        "evidence": evidence,
    }

    loop_case = SimpleNamespace(
        case_id=case.case_id, company=case.company, period=case.period,
        prediction_cutoff_date=case.prediction_cutoff_date,
        claims=list(claims), documents=list(docs), as_of_mode="historical_replay")

    res = agent_loop.run_loop(case=loop_case, client=client, mode="agent",
                              max_steps=max_steps, run_id=rid, kb=kb, facts=facts,
                              tools_ctx=tools_ctx)

    notes: list[str] = []
    state = res.state
    if res.exit == "fixed":
        notes.append("模型未配置或不可用，本次已回落固定链路（基线）。")
        fixed = run_fixed(case, root=root)
        state = _state_for(case, run_id=rid, mode="fixed",
                           claims=_claims_from_fixed_rows(fixed.raw.claims))
        atomic_write_json(run_dir(rid, create=True) / "state.json", state.to_dict())
        findings = fixed.findings
        gate = FindingGate(evidence=None, kb=None, state=state, page_texts=read,
                           assertions_by_id={}, require_refs=False)
        gate_res = gate.review(findings)
    else:
        if not state.claims:
            state.claims = list(claims)
        findings = _resolve_claims(state, facts=facts, kb=kb, run_id=rid)
        atomic_write_json(run_dir(rid, create=True) / "state.json", state.to_dict())  # 判定结果必须回写
        gate_res = FindingGate(
            evidence=evidence, kb=kb, state=state, page_texts=read,
            assertions_by_id={a.assertion_id: a for a in evidence.assertions()},
            require_refs=True).review(findings)
        notes.append("判定由确定性代码完成（模型只定调查路径）；发布由 V 门裁决。")

    payload = findings_payload(gate_res.findings, state, gate=gate_res, mode=state.mode,
                              extra={"documents": [d.to_dict() for d in docs],
                                     "notes": notes})
    out_dir = run_dir(rid, create=True)
    outputs = write_outputs(out_dir, payload)
    return Outcome(run_id=rid, run_dir=str(out_dir), mode=state.mode, exit=res.exit,
                   state=state, findings=list(gate_res.findings), gate=gate_res,
                   payload=payload, outputs=outputs, notes=notes)
