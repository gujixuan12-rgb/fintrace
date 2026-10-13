"""引用契约与 require_refs 堵漏（集成期补测）。

背景：V 门原先只从 audit / correction / evidence 的松散键里找引用。一条 Finding 只要
不引用任何断言，V5 就查不出问题、一路放行 —— 静默漏检。现在 Finding 有显式的
`assertion_refs` / `relation_refs`，并且 agent 模式建门时置 `require_refs=True`：
自称有材料支撑却零引用 → 待人工复核；fixed 基线（没有两轴模型）置 False，不受影响。
"""
from gate_factories import (
    make_assertion,
    make_finding,
    make_page_texts,
    make_state,
)

from fintrace_agent.contracts import (
    ApplicabilityField,
    MatchResult,
    Publish,
    Relation,
)
from fintrace_agent.gate.finding_gate import FindingGate


def _review(findings, *, require_refs=False, assertions_by_id=None):
    gate = FindingGate(
        evidence=None, kb=None, state=make_state(), page_texts=make_page_texts(),
        assertions_by_id=assertions_by_id or {}, require_refs=require_refs,
    )
    return gate.review(findings)


def test_explicit_assertion_refs_are_read():
    """显式字段必须被读：引用一条只过 W、未绑定的断言 → 待人工复核。"""
    a = make_assertion("A9", evidence_status="attested", semantic_status="unbound")
    f = make_finding()
    f.assertion_refs = ["A9"]
    out = _review([f], assertions_by_id={"A9": a}).findings[0]
    assert out.publish == Publish.MANUAL_REVIEW.value
    assert any("未通过双轴" in r for r in out.audit["v_manual"])


def test_fact_refs_do_not_block():
    """引用一条真事实时不得误伤。"""
    a = make_assertion("A1")                      # 双轴都过
    f = make_finding()
    f.assertion_refs = ["A1"]
    out = _review([f], assertions_by_id={"A1": a}).findings[0]
    assert out.publish == Publish.ALLOWED.value, out.block_reasons


def test_require_refs_flags_material_claim_without_refs():
    """自称有材料支撑（证据带值/原文）却零断言引用 → 不许静默放行。"""
    f = make_finding()
    out = _review([f], require_refs=True).findings[0]
    assert out.publish == Publish.MANUAL_REVIEW.value
    assert any("双轴无从核起" in r for r in out.audit["v_manual"])


def test_require_refs_off_keeps_baseline_usable():
    """fixed 基线没有两轴模型，置 False 时不得因缺引用被打成待复核。"""
    f = make_finding()
    out = _review([f], require_refs=False).findings[0]
    assert not any("双轴无从核起" in r for r in out.audit["v_manual"])


def test_require_refs_does_not_punish_findings_without_material_evidence():
    """定位不到、没有材料支撑的 INSUFFICIENT 不因缺引用被拉低。"""
    f = make_finding(verdict="INSUFFICIENT", evidence=[])
    out = _review([f], require_refs=True).findings[0]
    assert not any("双轴无从核起" in r for r in out.audit["v_manual"])


def test_quote_is_checked_fragment_by_fragment():
    """引文是单元格片段（引擎用「/」分隔）时，按片段逐段回查。

    拿整句做字面子串匹配会把正常结论整批拦掉（集成时真的发生过：15 条拦了 14 条）。
    每个实质片段都能在原件里找到即算过，片段形态只记提示、不阻断。
    """
    from gate_factories import make_evidence_dict

    ev = make_evidence_dict(quote="现金分红金额 / （元） / 营业收入")   # 三段，中间那段是单位
    out = _review([make_finding(evidence=[ev])]).findings[0]
    assert out.publish != Publish.BLOCKED.value, out.block_reasons
    notes = out.audit.get("v_checks", {}).get("V2", {}).get("notes") or []
    assert any("截断片段" in n for n in notes), notes


def test_quote_fragment_missing_from_page_is_blocked():
    """片段里只要有一个实质片段在原件找不到 → 仍然阻断（宽松不等于放水）。"""
    from gate_factories import make_evidence_dict

    ev = make_evidence_dict(quote="现金分红金额 / 营业收入是 123456789012 元")
    out = _review([make_finding(evidence=[ev])]).findings[0]
    assert out.publish == Publish.BLOCKED.value
    assert any("引文片段在原件里找不到" in r for r in out.block_reasons)


def test_numeric_fragment_must_match_with_digit_boundaries():
    """数字片段按数字边界核对：截断的数字不算命中（100 不许命中 1000 一类错误）。"""
    ev_dict = {"file_name": "2023年年度报告.pdf", "page": 12, "value": "27116214.63",
               "raw": "27,116,214.63", "quote": "现金分红金额 / 27,116,21"}
    out = _review([make_finding(evidence=[ev_dict])]).findings[0]
    assert out.publish == Publish.BLOCKED.value
    assert any("引文片段在原件里找不到" in r for r in out.block_reasons)


def test_value_missing_from_page_is_still_blocked():
    """值真的不在原件里时仍要拦 —— 宽松不等于放水。"""
    from gate_factories import make_evidence_dict

    f = make_finding(evidence=[make_evidence_dict(value="99999999.99")])
    out = _review([f]).findings[0]
    assert out.publish == Publish.BLOCKED.value
    assert any("值与原件对不上" in r for r in out.block_reasons)


def test_explicit_relation_refs_are_read():
    """显式关系引用必须被读：适用条件不成立 → 阻断该条。"""
    rel = Relation(
        relation_id="R9", predicate="contradicts", subject_ref="A1", object_ref="A2",
        applicability={"scheme_id": ApplicabilityField(
            claim="2025年度利润分配预案", evidence="S1", match=MatchResult.FAILED.value)},
    )
    f = make_finding()
    f.relation_refs = [rel]
    out = _review([f]).findings[0]
    assert any("适用条件不成立" in r for r in out.block_reasons)
    assert out.publish == Publish.BLOCKED.value
