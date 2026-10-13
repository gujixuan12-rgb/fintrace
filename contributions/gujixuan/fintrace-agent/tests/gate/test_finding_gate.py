"""模块 F 必测（二） —— gate/finding_gate.py 的 V1~V7 裁决。"""
from __future__ import annotations

from gate_factories import (
    FILE,
    PAGE,
    make_assertion,
    make_claim,
    make_evidence_dict,
    make_finding,
    make_page_texts,
    make_state,
)

from fintrace_agent.contracts import (
    ApplicabilityField,
    Assertion,
    MatchResult,
    Publish,
    Relation,
    Residual,
)
from fintrace_agent.gate.finding_gate import FindingGate


def build_gate(findings_state=None, *, pages=None, assertions=None, state=None,
               verifier=None, mode="pre_hoc"):
    return FindingGate(
        evidence=None,
        kb=None,
        state=state if state is not None else make_state(),
        page_texts=make_page_texts(pages),
        assertions_by_id=assertions or {},
        verifier=verifier,
        mode=mode,
    )


# ---------------------------------------------------------------------------
# V2 回查原件
# ---------------------------------------------------------------------------
def test_value_not_on_page_is_blocked():
    """值不在页 → blocked（不是 manual_review）。"""
    finding = make_finding(evidence=[make_evidence_dict(value="99999999.99")])
    gate = build_gate()
    result = gate.review([finding])

    assert result.findings[0].publish == Publish.BLOCKED.value
    assert any("值与原件对不上" in r for r in result.findings[0].block_reasons)
    assert result.counts["blocked"] == 1
    assert result.counts["manual_review"] == 0


def test_quote_not_on_page_is_blocked():
    """引文对不上原件 → blocked。"""
    finding = make_finding(evidence=[make_evidence_dict(quote="这句话原文里没有")])
    result = build_gate().review([finding])
    assert result.findings[0].publish == Publish.BLOCKED.value
    assert any("引文与原件对不上" in r for r in result.findings[0].block_reasons)


def test_page_unavailable_is_manual_review_with_residual():
    """取不到原文 → manual_review，并记一条 residual。"""
    finding = make_finding()
    gate = build_gate(pages={})  # 所有页都取不到
    result = gate.review([finding])

    assert result.findings[0].publish == Publish.MANUAL_REVIEW.value
    assert any("取不到原件" in r for r in result.findings[0].block_reasons)
    assert result.residuals, "取不到原文必须留下 residual（V6）"
    assert any(r.reason == "取不到原文" for r in result.residuals)


def test_page_texts_exception_treated_as_unavailable():
    """注入的取原文函数抛异常 → 按取不到处理（manual_review + residual）。"""

    def boom(file_name, page):
        raise RuntimeError("坏了的取原文函数")

    finding = make_finding()
    gate = FindingGate(evidence=None, kb=None, state=make_state(),
                       page_texts=boom, assertions_by_id={})
    result = gate.review([finding])
    assert result.findings[0].publish == Publish.MANUAL_REVIEW.value
    assert result.residuals


def test_contradicted_without_citation_is_blocked():
    """矛盾类判定必须带引用；无引用 → blocked（V2 的「引用存在」）。"""
    finding = make_finding(evidence=[])
    result = build_gate().review([finding])
    assert result.findings[0].publish == Publish.BLOCKED.value


# ---------------------------------------------------------------------------
# V5 语义绑定
# ---------------------------------------------------------------------------
def test_unbound_assertion_must_not_be_allowed():
    """值在页，但引用的 Assertion 未绑定（semantic_status != bound）→ 不得 allowed。"""
    finding = make_finding(audit={"assertion_ids": ["A2"]})
    unbound = make_assertion("A2", semantic_status="unbound")
    gate = build_gate(assertions={"A2": unbound})
    result = gate.review([finding])

    assert result.findings[0].publish != Publish.ALLOWED.value
    # 未绑定的只能出候选或 manual_review
    assert result.findings[0].publish == Publish.MANUAL_REVIEW.value
    assert any("未通过双轴" in r for r in result.findings[0].block_reasons)


def test_candidate_evidence_status_also_not_allowed():
    """过了语义绑定但没过 W 门（evidence_status != attested）同样不得 allowed。"""
    finding = make_finding(audit={"assertion_ids": ["A3"]})
    cand = make_assertion("A3", evidence_status="candidate")
    result = build_gate(assertions={"A3": cand}).review([finding])
    assert result.findings[0].publish == Publish.MANUAL_REVIEW.value


def test_is_fact_assertion_passes_v5():
    finding = make_finding(audit={"assertion_ids": ["A1"]})
    result = build_gate(assertions={"A1": make_assertion("A1")}).review([finding])
    assert result.findings[0].publish == Publish.ALLOWED.value


def test_assertion_from_evidence_log_is_resolved(fintrace_home):
    """assertions_by_id 里没有时，回落到 EvidenceLog 查断言。"""
    from fintrace_agent.store.evidence import EvidenceLog

    log = EvidenceLog("FT-20261010-bbbbbbbb")
    log.add_assertions([make_assertion("A9", semantic_status="unbound")])
    finding = make_finding(audit={"assertion_ids": ["A9"]})
    gate = FindingGate(evidence=log, kb=None, state=make_state(),
                       page_texts=make_page_texts(), assertions_by_id={})
    result = gate.review([finding])
    assert result.findings[0].publish == Publish.MANUAL_REVIEW.value


# ---------------------------------------------------------------------------
# 全绿
# ---------------------------------------------------------------------------
def test_all_green_is_allowed_with_risk_labels():
    """全绿 → allowed，且 report_labels 含 RISK_SIGNAL_DETECTED 与 FURTHER_VERIFICATION_SUGGESTED。"""
    finding = make_finding(audit={"assertion_ids": ["A1"]})
    state = make_state(claims=[make_claim("C1", status="verified")])
    gate = build_gate(assertions={"A1": make_assertion("A1")}, state=state)
    result = gate.review([finding])

    assert result.findings[0].publish == Publish.ALLOWED.value
    assert result.findings[0].block_reasons == []
    assert "RISK_SIGNAL_DETECTED" in result.findings[0].report_labels
    assert "FURTHER_VERIFICATION_SUGGESTED" in result.findings[0].report_labels
    assert result.counts == {"allowed": 1, "blocked": 0, "manual_review": 0}
    assert result.residuals == []


def test_supported_produces_no_risk_labels():
    """SUPPORTED 不产生风险标签（空列表）。"""
    finding = make_finding(verdict="SUPPORTED", audit={"assertion_ids": ["A1"]})
    result = build_gate(assertions={"A1": make_assertion("A1")}).review([finding])
    assert result.findings[0].publish == Publish.ALLOWED.value
    assert result.findings[0].report_labels == []


def test_post_hoc_authoritative_adds_confirmed_label():
    finding = make_finding(verdict="SUPPORTED", audit={
        "assertion_ids": ["A1"], "authoritative_doc": True})
    result = build_gate(assertions={"A1": make_assertion("A1")}, mode="post_hoc").review([finding])
    assert "CONFIRMED_BY_AUTHORITATIVE_DOCUMENT" in result.findings[0].report_labels


# ---------------------------------------------------------------------------
# V6 残余清单
# ---------------------------------------------------------------------------
def test_residuals_non_empty_when_there_are_residual_items():
    """结论全绿，但任务状态里有关键疑点 → GateResult.residuals 非空（V6）。"""
    state = make_state(claims=[make_claim("C9", status="open",
                                          open_questions=["供应商集中度是否异常？"])])
    result = build_gate(state=state).review([make_finding()])

    assert result.residuals, "有残余项时 residuals 必须非空"
    kinds = {r.kind for r in result.residuals}
    assert "unresolved_claim" in kinds
    assert "open_question" in kinds


def test_failed_tool_call_recorded_as_residual():
    state = make_state(steps=[{"action": "t_compute", "ok": False, "error": "boom"}])
    result = build_gate(state=state).review([make_finding()])
    assert any(r.kind == "failed_tool" for r in result.residuals)


def test_unauthorized_action_recorded_as_residual():
    state = make_state(
        steps=[{"action": "t_search_web", "ok": True}],
        allowed_tools=["t_compute", "t_parse"],
    )
    result = build_gate(state=state).review([make_finding()])
    assert any(r.kind == "unauthorized" for r in result.residuals)


def test_unfinished_step_recorded_as_residual():
    state = make_state(steps=[{"action": "t_recall", "status": "pending"}])
    result = build_gate(state=state).review([make_finding()])
    assert any(r.kind == "unfinished_step" for r in result.residuals)


def test_state_unresolved_is_carried_through():
    state = make_state(unresolved=[Residual(claim_id="C1", kind="blocked",
                                            text="原文不可得", reason="取不到原文")])
    result = build_gate(state=state).review([make_finding()])
    assert any(r.text == "原文不可得" for r in result.residuals)


# ---------------------------------------------------------------------------
# V1 结构
# ---------------------------------------------------------------------------
def test_missing_finding_id_is_blocked():
    finding = make_finding(finding_id="")
    result = build_gate().review([finding])
    assert result.findings[0].publish == Publish.BLOCKED.value
    assert any("finding_id" in r for r in result.findings[0].block_reasons)


def test_invalid_verdict_is_blocked_and_labels_empty():
    finding = make_finding(verdict="MAYBE")
    result = build_gate().review([finding])
    assert result.findings[0].publish == Publish.BLOCKED.value
    assert result.findings[0].report_labels == []


# ---------------------------------------------------------------------------
# V3 关系适用性
# ---------------------------------------------------------------------------
def _rel(rel_id: str, match: str, **extra) -> Relation:
    return Relation(
        relation_id=rel_id,
        predicate="equals",
        subject_ref="A1",
        object_ref="A2",
        applicability={"caliber": ApplicabilityField(
            claim="合并", evidence="合并", match=match)},
        rule_id="frm.demo",
        rule_version="v1",
        status="candidate",
        **extra,
    )


def test_inapplicable_relation_is_blocked():
    finding = make_finding(audit={"relations": [_rel("R1", MatchResult.FAILED.value)]})
    result = build_gate().review([finding])
    assert result.findings[0].publish == Publish.BLOCKED.value
    assert any("适用条件不成立" in r for r in result.findings[0].block_reasons)


def test_unknown_relation_is_manual_review():
    finding = make_finding(audit={"relations": [_rel("R2", MatchResult.UNKNOWN.value)]})
    result = build_gate().review([finding])
    assert result.findings[0].publish == Publish.MANUAL_REVIEW.value
    assert any("未验证" in r for r in result.findings[0].block_reasons)


def test_applicable_relation_passes():
    finding = make_finding(audit={
        "assertion_ids": ["A1"],
        "relations": [_rel("R3", MatchResult.VERIFIED.value)],
    })
    result = build_gate(assertions={"A1": make_assertion("A1")}).review([finding])
    assert result.findings[0].publish == Publish.ALLOWED.value


def test_relation_ref_without_applicability_is_manual_review():
    """只给了 relation_id、没有适用条件 → 无法判定 → manual_review。"""
    finding = make_finding(audit={"relations": ["R-unknown"]})
    result = build_gate().review([finding])
    assert result.findings[0].publish == Publish.MANUAL_REVIEW.value


def test_relation_accepts_dict_form():
    finding = make_finding(audit={"relations": [_rel("R4", MatchResult.FAILED.value).to_dict()]})
    result = build_gate().review([finding])
    assert result.findings[0].publish == Publish.BLOCKED.value


# ---------------------------------------------------------------------------
# V7 审查模型（只能拉低，不能放行）
# ---------------------------------------------------------------------------
def test_verifier_can_lower_but_not_raise():
    """审查模型能把 allowed 拉成 manual_review，但不能把 blocked 抬成 allowed。"""
    allowed = make_finding(audit={"assertion_ids": ["A1"]})
    noop = lambda payload: {"ok": True}  # noqa: E731
    result = FindingGate(evidence=None, kb=None, state=make_state(),
                         page_texts=make_page_texts(), assertions_by_id={"A1": make_assertion("A1")},
                         verifier=noop).review([allowed])
    assert result.findings[0].publish == Publish.ALLOWED.value

    flagged = make_finding(finding_id="F2", audit={"assertion_ids": ["A1"]})
    flagging = lambda payload: {"ok": False, "code": "doubt", "reason": "复算路径可疑"}  # noqa: E731
    result2 = FindingGate(evidence=None, kb=None, state=make_state(),
                          page_texts=make_page_texts(), assertions_by_id={"A1": make_assertion("A1")},
                          verifier=flagging).review([flagged])
    assert result2.findings[0].publish == Publish.MANUAL_REVIEW.value

    # blocked 的不会被审查模型抬起来
    blocked = make_finding(finding_id="F3", evidence=[make_evidence_dict(value="1.23")])
    result3 = FindingGate(evidence=None, kb=None, state=make_state(),
                          page_texts=make_page_texts(), assertions_by_id={},
                          verifier=noop).review([blocked])
    assert result3.findings[0].publish == Publish.BLOCKED.value
