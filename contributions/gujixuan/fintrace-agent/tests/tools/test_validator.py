"""证据真实性门 W1~W6（模块 E）。

钉住：
- 两轴分离：过 W 只到 ``evidence_status=attested``，``semantic_status`` 仍 ``unbound``，``is_fact`` 为 False。
- W2 时间硬门：晚于截止日的材料直接拒。
- W6 精度保真：``raw_text="0.155"`` 而 ``decimals_as_disclosed=2`` 必须被拒；
  ``raw_text="408,548.46"`` 而 ``decimals_as_disclosed=2`` 必须通过。
- 归置：不过的候选停在 candidate、进 rejected 明细，不进 accepted。
"""
from __future__ import annotations

from fintrace_agent.contracts import (
    Assertion, Document, EvidenceStatus, SemanticStatus, Span,
)
from fintrace_agent.tools.validator import Validator

_CUTOFF = "2024-04-22"
_FILE = "2023年年度报告.pdf"


def _doc(**kw) -> Document:
    base = dict(doc_id="D-1", source_id="", file_name=_FILE,
                source_type="original_annual_report", publication_date="2024-03-29")
    base.update(kw)
    return Document(**base)


def _pages(mapping: dict) -> "callable":
    """mapping: {(file_name, page): text}。"""
    def f(file_name, page):
        return mapping.get((file_name, page))
    return f


def _happy_span() -> Span:
    return Span(span_id="S-1", doc_id="D-1", page=51,
                quoted_text="现金分红金额（含税）408,548.46 元",
                bound_context={"row_label": "现金分红金额（含税）", "unit": "元"})


def _happy_pages() -> "callable":
    return _pages({(_FILE, 51): "……现金分红金额（含税） 408,548.46 元……"})


def _happy_assertion(**kw) -> Assertion:
    base = dict(
        assertion_id="A-1", kind="financial_value", subject="启明信息", period="2023A",
        metric="现金分红金额", value="408548.46", unit="元", raw_text="408,548.46",
        decimals_as_disclosed=2,
        text="2023年现金分红金额（含税）408,548.46元", source_spans=["S-1"],
    )
    base.update(kw)
    return Assertion(**base)


def _admit(kb, cands, spans, doc, page_texts, **vkw):
    v = Validator(kb=kb, cutoff_date=_CUTOFF, page_texts=page_texts, **vkw)
    return v.admit(cands, spans=spans, doc=doc)


# ---------------------------------------------------------------------------
# 不变量 1：两轴分离（过 W 不等于成事实）
# ---------------------------------------------------------------------------


def test_admit_attests_but_leaves_semantic_unbound(kb):
    a = _happy_assertion()
    out = _admit(kb, [a], [_happy_span()], _doc(), _happy_pages())

    assert [x.assertion_id for x in out.accepted] == ["A-1"]
    assert out.rejected == []
    assert a.evidence_status == EvidenceStatus.ATTESTED.value
    assert a.semantic_status == SemanticStatus.UNBOUND.value
    assert a.is_fact is False


def test_checks_report_all_six_steps(kb):
    out = _admit(kb, [_happy_assertion()], [_happy_span()], _doc(), _happy_pages())
    for code in ("W1", "W2", "W3", "W4", "W5", "W6"):
        assert code in out.checks
    assert out.checks["accepted"] == 1
    assert out.checks["rejected"] == 0
    assert out.checks["total"] == 1


# ---------------------------------------------------------------------------
# W1 格式
# ---------------------------------------------------------------------------


def test_w1_rejects_unparseable_value(kb):
    a = _happy_assertion(value="不是数字")
    out = _admit(kb, [a], [_happy_span()], _doc(), _happy_pages())
    assert out.accepted == []
    assert out.rejected[0]["code"] == "W1"


# ---------------------------------------------------------------------------
# W2 来源（时间硬门）
# ---------------------------------------------------------------------------


def test_w2_rejects_late_publication(kb):
    doc = _doc(publication_date="2024-04-23")   # cutoff 次日
    out = _admit(kb, [_happy_assertion()], [_happy_span()], doc, _happy_pages())
    assert out.accepted == []
    assert out.rejected[0]["code"] == "W2"


def test_w2_boundary_equal_to_cutoff_passes(kb):
    doc = _doc(publication_date=_CUTOFF)
    out = _admit(kb, [_happy_assertion()], [_happy_span()], doc, _happy_pages())
    assert [x.assertion_id for x in out.accepted] == ["A-1"]


def test_w2_rejects_source_type_outside_allowlist(kb):
    out = _admit(kb, [_happy_assertion()], [_happy_span()], _doc(), _happy_pages(),
                 allowed_source_types={"research_report"})
    assert out.rejected[0]["code"] == "W2"


# ---------------------------------------------------------------------------
# W3 身份
# ---------------------------------------------------------------------------


def test_w3_rejects_corrected_version(kb):
    doc = _doc(source_type="annual_report_corrected", file_name="2023年年度报告（更正版）.pdf")
    out = _admit(kb, [_happy_assertion()], [_happy_span()], doc, _happy_pages())
    assert out.rejected[0]["code"] == "W3"


def test_w3_rejects_unknown_period(kb):
    a = _happy_assertion(period="去年")
    out = _admit(kb, [a], [_happy_span()], _doc(), _happy_pages())
    assert out.rejected[0]["code"] == "W3"


# ---------------------------------------------------------------------------
# W4 数值五联
# ---------------------------------------------------------------------------


def test_w4_rejects_incompatible_unit(kb):
    a = _happy_assertion(unit="斤")
    out = _admit(kb, [a], [_happy_span()], _doc(), _happy_pages())
    assert out.rejected[0]["code"] == "W4"


def test_w4_rejects_number_not_on_bound_page(kb):
    # 引用页原文里查不到该数值 → 数值与页码未联合绑定。
    out = _admit(kb, [_happy_assertion()], [_happy_span()], _doc(), _pages({}))
    assert out.rejected[0]["code"] == "W4"


def test_w4_rejects_unknown_metric_name(kb):
    a = _happy_assertion(metric="星辰大海", metric_id="")
    out = _admit(kb, [a], [_happy_span()], _doc(), _happy_pages())
    assert out.rejected[0]["code"] == "W4"


# ---------------------------------------------------------------------------
# 不变量 3：W6 精度保真
# ---------------------------------------------------------------------------


def _per_ten_span() -> Span:
    return Span(span_id="S-6", doc_id="D-1", page=77,
                quoted_text="每10股派息数 0.155 元/10股",
                bound_context={"row_label": "每10股派息数", "unit": "元/10股"})


def _per_ten_assertion(**kw) -> Assertion:
    base = dict(
        assertion_id="A-6", kind="financial_value", subject="启明信息", period="2023A",
        metric="每10股派息数", value="0.155", unit="元/10股", raw_text="0.155",
        decimals_as_disclosed=3, text="2023年每10股派息数0.155元/10股", source_spans=["S-6"],
    )
    base.update(kw)
    return Assertion(**base)


_PER_TEN_PAGES = _pages({(_FILE, 77): "……每10股派息数 0.155 元/10股……"})


def test_w6_rejects_disclosed_decimals_mismatch(kb):
    # 披露位说 2 位，原文却是 0.155（3 位）→ 截断痕迹，必须拒。
    a = _per_ten_assertion(decimals_as_disclosed=2)
    out = _admit(kb, [a], [_per_ten_span()], _doc(), _PER_TEN_PAGES)
    assert out.accepted == []
    assert out.rejected[0]["code"] == "W6"


def test_w6_rejects_truncated_parsed_value(kb):
    # 原文 0.155，解析值被截成 0.15 → 必须拒。
    a = _per_ten_assertion(value="0.15", decimals_as_disclosed=3)
    out = _admit(kb, [a], [_per_ten_span()], _doc(), _PER_TEN_PAGES)
    assert out.accepted == []
    assert out.rejected[0]["code"] == "W6"


def test_w6_accepts_thousand_separated_two_decimals(kb):
    # raw="408,548.46" 且披露 2 位 → 通过。
    a = _happy_assertion()
    out = _admit(kb, [a], [_happy_span()], _doc(), _happy_pages())
    assert [x.assertion_id for x in out.accepted] == ["A-1"]
    assert out.checks["W6"] == 1


# ---------------------------------------------------------------------------
# W5 归置
# ---------------------------------------------------------------------------


def test_rejected_candidates_stay_candidate_and_not_accepted(kb):
    good = _happy_assertion(assertion_id="A-good")
    bad = _happy_assertion(assertion_id="A-bad", value="不是数字")
    out = _admit(kb, [good, bad], [_happy_span()], _doc(), _happy_pages())

    assert [x.assertion_id for x in out.accepted] == ["A-good"]
    assert out.rejected[0]["assertion_id"] == "A-bad"
    # 被拒的候选停在 candidate，绝不进事实库。
    assert bad.evidence_status == EvidenceStatus.CANDIDATE.value


def test_rejected_detail_carries_code_raw_and_reason(kb):
    a = _happy_assertion(unit="斤")
    out = _admit(kb, [a], [_happy_span()], _doc(), _happy_pages())
    rec = out.rejected[0]
    assert rec["code"] == "W4"
    assert rec["raw"] == "408,548.46"
    assert rec["why"]
    assert rec.get("span_sha")
