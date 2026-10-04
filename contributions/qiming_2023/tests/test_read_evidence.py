"""Safety boundaries for the two-page reader and its single supported claim.

Run with the repository's src on PYTHONPATH. Set FINTRACE_QIMING_PDF to the
original annual-report PDF to enable the real-document integration test.
Synthetic pages below exercise rejection boundaries, not financial facts.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path

import pytest


HERE = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("qi_feiyang_read_evidence", HERE / "read_evidence.py")
reader = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reader)

PAGE_51 = """启明信息技术股份有限公司2023年年度报告全文
本报告期利润分配及资本公积金转增股本情况
每 10股派息数（元）（含税） 0.10
分配预案的股本基数（股） 408548455
现金分红金额（元）（含税） 408,548.46
十一、公司股权激励
51
"""
PAGE_223 = """启明信息技术股份有限公司2023年年度报告全文
2、利润分配情况
利润分配方案 向全体股东按每 10 股派发现金红利 0.10 元（含税），利润
分配总额为 408.55 万元，其他未分配利润暂不分配。
223
"""


class Page:
    def __init__(self, text):
        self.text = text

    def extract_text(self, *, extraction_mode):
        assert extraction_mode == "layout"
        return self.text


class Pages:
    def __init__(self, text_51=PAGE_51, text_223=PAGE_223, count=237):
        self.texts = {50: text_51, 222: text_223}
        self.count = count
        self.accesses = []

    def __len__(self):
        return self.count

    def __getitem__(self, index):
        self.accesses.append(index + 1)
        if index not in self.texts:
            raise AssertionError(f"Unexpected page access: {index + 1}")
        return Page(self.texts[index])


class Document:
    def __init__(self, **kwargs):
        self.pages = Pages(**kwargs)


@pytest.fixture
def claim():
    return {
        "claim_id": "QM-TEST-001",
        "claim_text": "启明信息在2023年年度报告披露的利润分配预案为每10股派发现金红利0.10元（含税）。",
        "subject": "启明信息技术股份有限公司",
        "period": "2023A",
        "metric": "dividend_per_ten_shares",
        "value": "0.10",
        "unit": "元/10股",
        "tax_basis": "含税",
        "distribution_status": "proposed",
        "scope": "上市公司向全体股东的利润分配预案",
    }


@pytest.fixture
def payload():
    return {
        "company": "启明信息技术股份有限公司",
        "stock_code": "002232",
        "prediction_cutoff_date": "2024-04-22",
        "pre_cutoff_source": {
            "source_id": "QM-ORIGINAL-2023",
            "file_name": "original.pdf",
            "source_type": "original_annual_report",
            "source_version": "original",
            "publication_date": "2024-04-13",
            "source_url": "https://static.cninfo.com.cn/finalpage/2024-04-13/1219596126.PDF",
            "publication_date_verification": {"status": "official_page_verified", "date": "2024-04-13"},
            "sha256": reader.VERIFIED_SHA256,
            "bytes": 4369829,
        },
        "observations": {
            "shares": {"value": "408548455", "unit": "股", "printed_page": 51},
            "dividend_per_ten_shares": {"value": "0.10", "unit": "元/10股", "printed_page": 51},
            "disclosed_dividend": {"value": "408548.46", "unit": "元", "printed_page": 51},
            "other_page_total": {"value": "408.55", "unit": "万元", "printed_page": 223},
        },
    }


@pytest.fixture
def root_gate():
    pytest.importorskip("fintrace.verification.cutoff_filter", reason="Set PYTHONPATH to the root repository src")
    return reader.project_gate


@pytest.fixture
def synthetic_pdf(tmp_path, monkeypatch, payload):
    path = tmp_path / "original.pdf"
    data = b"synthetic-document-fixture".ljust(4369829, b"\0")
    path.write_bytes(data)
    # Unit tests bind their own fixture to the trusted hash; production keeps
    # its original-report pin. Hash calculation and mismatch rejection run.
    digest = hashlib.sha256(data).hexdigest()
    monkeypatch.setattr(reader, "VERIFIED_SHA256", digest)
    payload["pre_cutoff_source"]["sha256"] = digest
    document = Document()
    calls = []

    def parse(data, audit=None):
        calls.append(len(data))
        return reader._extract_pages(document, audit)

    monkeypatch.setattr(reader, "extract_pages", parse)
    return path, document, calls


def forbid_pdf_read(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Rejected input must not read any PDF bytes")
    monkeypatch.setattr(Path, "read_bytes", forbidden)


@pytest.mark.parametrize("where,value", [
    ("source", None), ("source", ""), ("source", "2024-02-30"),
    ("source", "2024-04-13T00:00:00"), ("cutoff", "2024/04/22"),
    ("cutoff", None),
])
def test_invalid_dates_fail_before_read(payload, claim, root_gate, monkeypatch, where, value):
    if where == "source":
        payload["pre_cutoff_source"]["publication_date"] = value
    else:
        payload["prediction_cutoff_date"] = value
    forbid_pdf_read(monkeypatch)
    result = reader.run(payload, claim, "unused.pdf", gate=root_gate)
    assert result["status"] == "INSUFFICIENT_INFORMATION"
    assert result["pdf_content_read"] is False
    assert result["file_accesses"] == []


def test_late_document_uses_root_gate_and_never_reads(payload, claim, root_gate, monkeypatch):
    payload["pre_cutoff_source"]["publication_date"] = "2024-04-23"
    forbid_pdf_read(monkeypatch)
    result = reader.run(payload, claim, "unused.pdf", gate=root_gate)
    assert result["status"] == "NOT_ELIGIBLE"
    assert result["pdf_pages_read"] == []
    assert result["tool_calls"][0]["tool"] == "fintrace.verification.cutoff_filter.filter_record"
    assert result["tool_calls"][0]["eligible"] is False


@pytest.mark.parametrize("change", ["answer", "nested_answer", "claim_answer", "corrected_version", "correction_type"])
def test_answers_and_later_versions_are_rejected(payload, claim, root_gate, monkeypatch, change):
    if change == "answer":
        payload["post_cutoff_validation"] = {"do_not_use": True}
    elif change == "nested_answer":
        payload["observations"]["shares"]["expected_value"] = "1"
    elif change == "claim_answer":
        claim["expected_label"] = "SUPPORTED"
    elif change == "corrected_version":
        payload["pre_cutoff_source"]["source_version"] = "corrected"
    else:
        payload["pre_cutoff_source"]["source_type"] = "correction_announcement"
    forbid_pdf_read(monkeypatch)
    result = reader.run(payload, claim, "unused.pdf", gate=root_gate)
    assert result["status"] == "INSUFFICIENT_INFORMATION"
    assert result["file_accesses"] == []


def test_hash_mismatch_reads_bytes_but_does_not_parse(payload, claim, root_gate, synthetic_pdf):
    path, document, calls = synthetic_pdf
    path.write_bytes(b"altered-document".ljust(4369829, b"\0"))
    result = reader.run(payload, claim, path, gate=root_gate)
    assert result["status"] == "INSUFFICIENT_INFORMATION"
    assert result["file_accesses"] == [{"file_name": "original.pdf", "purpose": "hash_and_size_verification"}]
    assert result["pdf_content_read"] is False
    assert calls == document.pages.accesses == []


def test_manifest_cannot_rebind_known_pdf_hash(payload, claim, root_gate, monkeypatch):
    payload["pre_cutoff_source"]["sha256"] = "0" * 64
    forbid_pdf_read(monkeypatch)
    result = reader.run(payload, claim, "unused.pdf", gate=root_gate)
    assert result["status"] == "INSUFFICIENT_INFORMATION"
    assert result["file_accesses"] == []


@pytest.mark.parametrize("field,replacement", [("value", "0.01"), ("unit", "元/股"), ("printed_page", 52)])
def test_manual_excerpt_disagreement_is_not_silently_used(payload, claim, root_gate, synthetic_pdf, field, replacement):
    path, document, _ = synthetic_pdf
    payload["observations"]["dividend_per_ten_shares"][field] = replacement
    result = reader.run(payload, claim, path, gate=root_gate)
    assert result["status"] == "INSUFFICIENT_INFORMATION"
    assert result["claim_check"] is None
    assert result["pdf_pages_read"] == document.pages.accesses == [51, 223]


def test_supported_claim_keeps_other_reported_amount_separate(payload, claim, root_gate, synthetic_pdf):
    path, document, _ = synthetic_pdf
    result = reader.run(payload, claim, path, gate=root_gate)
    assert result["status"] == "EVIDENCE_READY"
    assert result["claim_check"]["status"] == "SUPPORTED"
    assert result["claim_check"]["flag_as_error"] is False
    assert result["observations"]["disclosed_dividend"]["value"] == "408548.46"
    assert result["pdf_pages_read"] == document.pages.accesses == [51, 223]
    assert result["evidence_refs"][1]["quoted_text"] in PAGE_223
    assert result["tool_calls"][0]["eligible"] is True


@pytest.mark.parametrize("value,status,flag", [("0.1", "SUPPORTED", False), ("0.01", "VALUE_MISMATCH", True)])
def test_claim_values_are_compared_with_decimal(claim, value, status, flag):
    _, fields = reader._extract_pages(Document())
    claim["value"] = value
    claim["claim_text"] = claim["claim_text"].replace("0.10", value)
    checked = reader.check_claim(claim, fields)
    assert checked["status"] == status
    assert checked["flag_as_error"] is flag


@pytest.mark.parametrize("field,replacement", [
    ("claim_text", "启明信息在2023年已实际每10股派发现金红利0.10元（含税）。"),
    ("distribution_status", "paid"), ("period", "2024A"),
    ("unit", "元/股"), ("value", "0.01"),
])
def test_unsupported_or_inconsistent_semantics_do_not_pass(claim, field, replacement):
    _, fields = reader._extract_pages(Document())
    claim[field] = replacement
    checked = reader.check_claim(claim, fields)
    assert checked["status"] == "INSUFFICIENT_INFORMATION"
    assert checked["flag_as_error"] is False


@pytest.mark.parametrize("mutation,expected_pages", [
    ("wrong_page_51", [51]), ("wrong_page_223", [51, 223]),
    ("duplicate_row", [51, 223]), ("duplicate_total", [51, 223]),
    ("different_per_ten", [51, 223]), ("wrong_page_count", []),
])
def test_parser_rejects_ambiguous_pages_and_preserves_failure_audit(mutation, expected_pages):
    a, b, count = PAGE_51, PAGE_223, 237
    if mutation == "wrong_page_51":
        a = a.removesuffix("51\n") + "50\n"
    elif mutation == "wrong_page_223":
        b = b.removesuffix("223\n") + "222\n"
    elif mutation == "duplicate_row":
        a = a.replace("现金分红金额（元）（含税） 408,548.46", "现金分红金额（元）（含税） 408,548.46\n现金分红金额（元）（含税） 408,548.46")
    elif mutation == "duplicate_total":
        b = b.replace("223\n", "利润分配总额为 408.55 万元。\n223\n")
    elif mutation == "different_per_ten":
        b = b.replace("0.10", "0.01")
    else:
        count = 236
    document = Document(text_51=a, text_223=b, count=count)
    audit = {"pdf_content_read": False, "pdf_pages_read": []}
    with pytest.raises(ValueError):
        reader._extract_pages(document, audit)
    assert audit["pdf_pages_read"] == document.pages.accesses == expected_pages
    assert audit["pdf_content_read"] is bool(expected_pages)


def test_failed_second_page_read_is_visible_in_run(payload, claim, root_gate, synthetic_pdf):
    path, document, _ = synthetic_pdf
    document.pages.texts[222] = PAGE_223.removesuffix("223\n") + "222\n"
    result = reader.run(payload, claim, path, gate=root_gate)
    assert result["status"] == "INSUFFICIENT_INFORMATION"
    assert result["pdf_content_read"] is True
    assert result["pdf_pages_read"] == [51, 223]
    assert "第223页" in result["reason"]


@pytest.mark.skipif(not os.environ.get("FINTRACE_QIMING_PDF"), reason="Set FINTRACE_QIMING_PDF to the original PDF")
def test_real_original_pdf_matches_delivered_input_and_claim(root_gate):
    payload = json.loads((HERE / "qiming_2023_dividend.json").read_text(encoding="utf-8"))
    claim = json.loads((HERE / "claim_input.json").read_text(encoding="utf-8"))
    result = reader.run(payload, claim, Path(os.environ["FINTRACE_QIMING_PDF"]), gate=root_gate)
    assert result["status"] == "EVIDENCE_READY", result.get("reason")
    assert result["pdf_pages_read"] == [51, 223]
    assert result["claim_check"]["status"] == "SUPPORTED"
    assert result["claim_check"]["flag_as_error"] is False
    assert result["source_sha256"] == payload["pre_cutoff_source"]["sha256"]
    assert {e["page"] for e in result["evidence_refs"]} == {51, 223}
