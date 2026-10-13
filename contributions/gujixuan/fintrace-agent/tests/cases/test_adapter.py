"""案件适配自检（v2.2 §9）：真实案件、真实草稿，不碰 PDF。"""
from __future__ import annotations

from fintrace_agent.cases.adapter import (
    draft_to_claims,
    find_case,
    list_cases,
    load_case,
    read_expected,
    documents_of,
    read_draft,
)


def test_two_cases_are_discoverable():
    ids = list_cases()
    assert "qiming_2023_draft_case" in ids
    assert "tianxiaxiu_2023_draft_case" in ids


def test_load_case_by_case_id_and_by_stem():
    a = load_case("data/annotations/cases/qiming_2023_draft_case.json")
    b = find_case("QM-DRAFT-2023-001")
    assert a.case_id == b.case_id == "QM-DRAFT-2023-001"
    assert b.stock_code == "002232"
    assert b.prediction_cutoff_date == "2024-04-22"


def test_cutoff_case_is_really_a_leakage_case():
    """这个案子存在的意义就是测时间截断：更正公告晚于截止日一天。"""
    case = find_case("QM-DRAFT-2023-001")
    docs = documents_of(case)
    assert len(docs) == 2
    late = [d for d in docs if d.publication_date > case.prediction_cutoff_date]
    assert late and late[0].source_type == "correction_announcement"
    assert late[0].publication_date == "2024-04-23"
    assert case.prediction_cutoff_date == "2024-04-22"


def test_documents_carry_absolute_paths_and_hash():
    case = find_case("QM-DRAFT-2023-001")
    for d in documents_of(case):
        assert d.content_path and ":\\" in d.content_path, "路径必须是绝对路径"
        assert len(d.content_hash) == 64, f"{d.file_name} 没算 content_hash"
        assert d.material_format == "pdf"
        assert d.first_published_at == d.publication_date


def test_draft_to_claims_non_empty_and_shape_valid():
    case = find_case("QM-DRAFT-2023-001")
    text = read_draft(case)
    assert len(text) > 200

    claims = draft_to_claims(text, case=case)
    assert claims, "拆不出主张说明适配器坏了"
    for c in claims:
        assert c.claim_id.startswith("C")
        assert c.claim_text.strip(), "主张正文不能为空"
        assert not c.claim_text.startswith("#"), "markdown 标题残留没清掉"
        assert c.subject == case.company
    assert any(c.metric_key for c in claims), "至少要有一条绑定了指标键"
    assert any(c.claim_value for c in claims), "至少要有一条带数值"


def test_draft_to_claims_is_deterministic():
    """同一草稿两次拆解必须逐字相同 —— 规则拆解是可复现性的底。"""
    case = find_case("tianxiaxiu_2023_draft_case")
    text = read_draft(case)
    a = [c.to_dict() for c in draft_to_claims(text, case=case)]
    b = [c.to_dict() for c in draft_to_claims(text, case=case)]
    assert a == b


def test_expected_file_is_readable_for_metrics():
    case = find_case("QM-DRAFT-2023-001")
    exp = read_expected(case)
    assert exp.get("claims"), "真值文件读不出 claims"
    assert exp.get("annotation_scope"), "金标是否声明完整必须可读（否则不得外推无误报）"
