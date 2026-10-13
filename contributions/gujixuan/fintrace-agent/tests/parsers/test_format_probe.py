"""材料格式探测（v2.2 §5.3）：四类魔数各一例 + 认不出返回 unknown。"""
from __future__ import annotations

from pathlib import Path

from fintrace_agent.contracts import MaterialFormat
from fintrace_agent.parsers.format_probe import probe


def test_probe_pdf(tmp_path: Path):
    p = tmp_path / "年报.pdf"
    p.write_bytes(b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\n")
    r = probe(p)
    assert r["format"] == MaterialFormat.PDF.value
    assert "PDF" in r["signature"].upper() or "%PDF" in r["signature"]


def test_probe_docx(sample_docx: Path):
    r = probe(sample_docx)
    assert r["format"] == MaterialFormat.DOCX.value
    assert "word/document.xml" in r["signature"]


def test_probe_doc(tmp_path: Path):
    p = tmp_path / "旧稿.doc"
    p.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64)
    r = probe(p)
    assert r["format"] == MaterialFormat.DOC.value


def test_probe_text(tmp_path: Path):
    p = tmp_path / "草稿.txt"
    p.write_text("预计2024年营业收入增长。\n第二段。\n", encoding="utf-8")
    r = probe(p)
    assert r["format"] == MaterialFormat.TEXT.value


def test_probe_zip_without_document_xml_is_unknown(tmp_path: Path):
    """普通 zip 不是 docx —— 不许当成 docx，也不许当成空材料。"""
    import zipfile

    p = tmp_path / "data.zip"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("readme.txt", "hi")
    r = probe(p)
    assert r["format"] == MaterialFormat.UNKNOWN.value


def test_probe_binary_with_nul_is_unknown(tmp_path: Path):
    p = tmp_path / "blob.bin"
    p.write_bytes(bytes(range(256)))
    r = probe(p)
    assert r["format"] == MaterialFormat.UNKNOWN.value


def test_probe_missing_file_is_unknown(tmp_path: Path):
    r = probe(tmp_path / "不存在.pdf")
    assert r["format"] == MaterialFormat.UNKNOWN.value


def test_probe_returns_why_and_signature_keys(tmp_path: Path):
    p = tmp_path / "x.pdf"
    p.write_bytes(b"%PDF-1.4\n")
    r = probe(p)
    assert set(r) == {"format", "why", "signature"}
    assert isinstance(r["why"], str) and r["why"]


def test_docx_probe_matches_extract_end_to_end(sample_docx: Path):
    """探测说 docx，抽取就得抽得出东西（避免探测/抽取口径打架）。"""
    from fintrace_agent.parsers.docx_extract import extract_spans

    assert probe(sample_docx)["format"] == MaterialFormat.DOCX.value
    spans = extract_spans(sample_docx, "D-1")
    assert spans, "探测为 docx 却抽不出 Span"
