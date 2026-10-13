"""docx 抽取（v2.2 §5.3）：段落/表格行产出 Span，row_label、单调 char_range、降级为空。"""
from __future__ import annotations

from pathlib import Path

from fintrace_agent.contracts import ExtractorTier, SpanKind
from fintrace_agent.parsers.docx_extract import extract_spans


def test_extract_paragraphs_and_table_rows(sample_docx: Path):
    spans = extract_spans(sample_docx, "D-1")
    kinds = [s.span_kind for s in spans]
    assert kinds == [
        SpanKind.PARAGRAPH.value,
        SpanKind.PARAGRAPH.value,
        SpanKind.TABLE_ROW.value,
        SpanKind.TABLE_ROW.value,
    ], kinds


def test_extract_paragraph_text_joins_runs(sample_docx: Path):
    spans = extract_spans(sample_docx, "D-1")
    # 第二段的两个 run 必须拼成一个段落
    assert spans[1].quoted_text == "预计2024年营业收入增长"
    assert "408,548.46" in spans[0].quoted_text


def test_extract_table_row_row_label_is_first_cell(sample_docx: Path):
    spans = extract_spans(sample_docx, "D-1")
    rows = [s for s in spans if s.span_kind == SpanKind.TABLE_ROW.value]
    assert rows[0].bound_context.get("row_label") == "现金分红金额（含税）"
    assert rows[1].bound_context.get("row_label") == "每股收益"
    # 单元格按 w:tc 拼接
    assert rows[0].quoted_text == "现金分红金额（含税） | 408,548.46 | 元"


def test_extract_char_range_is_monotonic(sample_docx: Path):
    spans = extract_spans(sample_docx, "D-1")
    start = -1
    for s in spans:
        lo, hi = s.char_range
        assert isinstance(s.char_range, tuple) and lo <= hi
        assert lo >= start, f"char_range 不单调：{s.char_range}"
        start = hi


def test_extract_sets_extractor_and_doc_id(sample_docx: Path):
    spans = extract_spans(sample_docx, "D-9")
    assert all(s.doc_id == "D-9" for s in spans)
    assert all(s.extractor == ExtractorTier.DOCX.value for s in spans)
    assert all(s.page is None for s in spans)


def test_extract_start_span_offsets_ids(sample_docx: Path):
    spans = extract_spans(sample_docx, "D-1", start_span=100)
    assert spans[0].span_id == "S-000100"
    assert spans[1].span_id == "S-000101"


def test_extract_non_number_span_has_no_decimals(sample_docx: Path):
    spans = extract_spans(sample_docx, "D-1")
    assert spans[0].decimals_as_disclosed is None


def test_extract_missing_file_returns_empty(tmp_path: Path):
    assert extract_spans(tmp_path / "没有.docx", "D-1") == []


def test_extract_bad_zip_returns_empty(tmp_path: Path):
    p = tmp_path / "坏.docx"
    p.write_bytes(b"not a zip at all")
    assert extract_spans(p, "D-1") == []


def test_extract_zip_without_document_xml_returns_empty(tmp_path: Path):
    import zipfile

    p = tmp_path / "空.docx"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("word/styles.xml", "<x/>")
    assert extract_spans(p, "D-1") == []
