"""契约自检（v2.2 §2、§3、§13）：两轴状态、适用条件、标签映射、状态往返。

这一层是防线：契约错了，上面所有模块都会跟着错，所以先把它钉死。
"""
from __future__ import annotations

import json

from fintrace_agent import paths
from fintrace_agent.contracts import (
    ACTIONS,
    ACTIONS_DISABLED_BY_DEFAULT,
    LABEL_MAP,
    ApplicabilityField,
    Assertion,
    Claim,
    ClaimStatus,
    Document,
    EvidenceStatus,
    MatchResult,
    Relation,
    RelationPredicate,
    ReportLabel,
    SemanticStatus,
    Span,
    TaskState,
    Verdict,
    digest,
    sha256_text,
)


def _assertion(**kw) -> Assertion:
    base = dict(assertion_id="A-1", kind="financial_value", subject="启明信息", period="2023A",
                metric="现金分红金额", value="408548.46", unit="元", raw_text="408,548.46")
    base.update(kw)
    return Assertion(**base)


def test_two_axis_requires_both():
    """只过 W（attested）而语义未绑定的，不算事实，也不得支撑矛盾。"""
    a = _assertion()
    assert a.evidence_status == EvidenceStatus.CANDIDATE.value
    assert a.semantic_status == SemanticStatus.UNBOUND.value
    assert not a.is_fact and not a.can_support_contradiction

    a.evidence_status = EvidenceStatus.ATTESTED.value
    assert not a.is_fact, "只有 evidence 一轴不算事实"

    a.semantic_status = SemanticStatus.BOUND.value
    assert a.is_fact and a.can_support_contradiction


def test_engine_origin_never_supports_contradiction():
    """复算值（engine）永远不当出处。"""
    a = _assertion(origin="engine", evidence_status=EvidenceStatus.ATTESTED.value,
                   semantic_status=SemanticStatus.BOUND.value)
    assert a.is_fact and not a.can_support_contradiction


def test_applicability_requires_all_fields_verified():
    """适用条件：任一 failed 即不适用，unknown 也不得判矛盾（v2.2 §2.4）。"""
    r = Relation(relation_id="R-1", predicate=RelationPredicate.CONTRADICTS.value,
                 subject_ref="A-1", object_ref="A-2")
    assert not r.applicable, "空适用条件不得通过"

    r.applicability = {
        "period": ApplicabilityField(claim="2025A", evidence="2025A", match=MatchResult.VERIFIED.value),
        "scheme_id": ApplicabilityField(claim="2025年度利润分配预案",
                                        evidence="S-71", match=MatchResult.FAILED.value),
    }
    assert not r.applicable
    assert r.failed_fields == ["scheme_id"]
    assert r.field_ok("period")

    r.applicability["scheme_id"] = ApplicabilityField(claim="a", evidence="b",
                                                     match=MatchResult.UNKNOWN.value)
    assert not r.applicable, "unknown 视为未验证"


def test_applicability_roundtrip():
    r = Relation(relation_id="R-2", predicate=RelationPredicate.EQUALS.value,
                 subject_ref="A-1", object_ref="A-2",
                 applicability={"base": ApplicabilityField(claim="2429119230",
                                                           evidence="2429119230",
                                                           match=MatchResult.VERIFIED.value)})
    back = Relation.from_dict(json.loads(json.dumps(r.to_dict(), ensure_ascii=False)))
    assert isinstance(back.applicability["base"], ApplicabilityField)
    assert back.applicable and back.field_ok("base")


def test_label_map_covers_all_verdicts():
    """四类判定都要有映射（可以不产标签，但不能没有条目）。"""
    for v in Verdict:
        assert v in LABEL_MAP, f"{v} 没有映射条目"
    assert LABEL_MAP[Verdict.CONTRADICTED] == ReportLabel.RISK_SIGNAL_DETECTED
    assert LABEL_MAP[Verdict.SUPPORTED] is None
    assert LABEL_MAP[Verdict.PREDICTION] is None
    assert LABEL_MAP[Verdict.INSUFFICIENT] == ReportLabel.INSUFFICIENT_INFORMATION


def test_taskstate_roundtrip_keeps_claims_and_axes():
    st = TaskState(run_id="FT-20261010-abcdef01", goal={"task": "draft_review",
                                                        "case_id": "QM-DRAFT-2023-001"},
                   as_of="2024-04-22")
    st.claims.append(Claim(claim_id="C001", claim_text="2023 年度现金分红 408,548.46 元",
                           status=ClaimStatus.OPEN.value, open_questions=["方案口径未定"]))
    st.evidence_index.append("A-1")
    st.unresolved.append(__import__("fintrace_agent.contracts", fromlist=["Residual"]).Residual(
        claim_id="C001", kind="open_question", text="方案口径未定"))

    back = TaskState.from_dict(json.loads(json.dumps(st.to_dict(), ensure_ascii=False)))
    assert back.run_id == st.run_id
    assert back.claims[0].claim_id == "C001"
    assert back.claims[0].open_questions == ["方案口径未定"]
    assert not back.claims[0].resolved
    assert back.open_claims and len(back.open_claims) == 1
    assert back.unresolved[0].kind == "open_question"
    assert back.bounds.cutoff_date == ""
    assert back.bounds.allowed_tools == list(st.bounds.allowed_tools)


def test_claim_resolved_states():
    c = Claim(claim_id="C1", claim_text="x")
    for s in (ClaimStatus.VERIFIED.value, ClaimStatus.INSUFFICIENT.value, ClaimStatus.BLOCKED.value):
        c.status = s
        assert c.resolved, s
    c.status = ClaimStatus.OPEN.value
    assert not c.resolved


def test_digest_is_stable_and_order_independent():
    a = {"b": 2, "a": 1, "list": [1, 2]}
    b = {"a": 1, "list": [1, 2], "b": 2}
    assert digest(a) == digest(b), "同一内容必须给出同一 digest（可复现性依赖它）"
    assert len(digest(a)) == 16
    assert sha256_text("x") != sha256_text("y")


def test_span_roundtrip_converts_tuples():
    s = Span(span_id="S-1", doc_id="D-1", page=51, char_range=(1204, 1231),
             quoted_text="408,548.46", decimals_as_disclosed=2,
             bound_context={"row_label": "现金分红金额（含税）", "unit": "元"})
    back = Span.from_dict(s.to_dict())
    assert isinstance(back.char_range, tuple) and back.char_range == (1204, 1231)
    assert back.decimals_as_disclosed == 2
    assert back.bound_context["unit"] == "元"


def test_document_roundtrip_and_first_published_at():
    d = Document(doc_id="D-1", source_id="src-1", file_name="2023年年度报告.pdf",
                 source_type="original_annual_report", publication_date="2024-03-29",
                 page_count=237, first_published_at="2024-04-22", material_format="pdf")
    back = Document.from_dict(d.to_dict())
    assert back == d


def test_actions_whitelist_and_author_tool_disabled():
    assert "t_author_tool" in ACTIONS and "t_spec_extract" in ACTIONS and "stop" in ACTIONS
    b = TaskState(run_id="r").bounds
    assert "t_author_tool" not in b.allowed_tools
    assert set(ACTIONS_DISABLED_BY_DEFAULT) == {"t_author_tool"}
    assert b.as_of_mode == "historical_replay"


def test_paths_single_source_of_truth():
    """项目资产跟项目根；运行时产物跟 FINTRACE_HOME（缺省项目根），且只有一处解析。"""
    assert paths.ROOT.name == "fintrace-agent"
    for p in (paths.CASES_DIR, paths.KNOWLEDGE_DB, paths.FACTS_DB):
        assert str(p).startswith(str(paths.ROOT)), f"{p} 跑到项目外面去了"
    assert paths.KNOWLEDGE_DB.name == "knowledge.sqlite"

    assert paths.home() == paths.ROOT, "缺省根必须是项目根，不能是当前工作目录"
    assert paths.runs_dir() == paths.ROOT / "runs"

    # store 包必须用同一个解析，不许自己再算一遍（历史上它默认取 cwd）
    from fintrace_agent.store import paths as store_paths

    assert store_paths.home() == paths.home()
    assert store_paths.runs_dir() == paths.runs_dir()
    assert store_paths.blobs_dir() == paths.blobs_dir()
    assert store_paths.snapshots_dir() == paths.snapshots_dir()


def test_runtime_dirs_follow_fintrace_home(monkeypatch, tmp_path):
    """重定向靠环境变量，一个开关管住 store 包与主包。"""
    monkeypatch.setenv("FINTRACE_HOME", str(tmp_path))
    assert paths.home() == tmp_path
    assert paths.runs_dir() == tmp_path / "runs"
    assert paths.run_dir("FT-1", create=True).is_dir()
    assert (tmp_path / "runs" / "FT-1" / "snapshots").is_dir()

    from fintrace_agent.store import paths as store_paths

    assert store_paths.blobs_dir() == tmp_path / "blobs"
