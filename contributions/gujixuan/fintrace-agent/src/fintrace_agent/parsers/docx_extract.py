"""docx 抽取（v2.2 §5.3）：零第三方依赖读 Word 正文与表格。

只用标准库 ``zipfile`` + ``xml.etree.ElementTree``：读 ``word/document.xml``，
按段落（``w:p``）与表格行（``w:tr``）产出 Span。

- 段落 → ``span_kind="paragraph"``，文本按文档顺序拼所有 ``w:t``；
- 表格行 → ``span_kind="table_row"``，单元格文本按 ``w:tc`` 拼接，
  ``bound_context.row_label`` 取该行第一个单元格文本；
- ``char_range`` 用该片段在文档文本流中的**累计偏移**（近似，但严格单调不减）。

读不出（坏 zip / 缺 document.xml / 非法 XML）一律降级返回空列表，不抛异常。
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from ..contracts import ExtractorTier, Span, SpanKind
from .bridge import decimals_of

#: OOXML 主命名空间
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

#: docx 正文条目
DOCUMENT_PART = "word/document.xml"

#: 表格行内单元格的默认拼接符（人类可读，也便于下游按分隔符切格）
_CELL_SEP = " | "


def _w(tag: str) -> str:
    return f"{{{W_NS}}}{tag}"


def _para_text(node: ET.Element) -> str:
    """段落文本：按文档顺序拼接段内所有 ``w:t``。"""
    return "".join(t.text or "" for t in node.iter(_w("t")))


def _row_cells(row: ET.Element) -> list[str]:
    """表格行：逐 ``w:tc`` 取文本（含单元格内多段）。"""
    cells: list[str] = []
    for tc in row.findall(_w("tc")):
        cells.append("".join(_para_text(p) for p in tc.findall(_w("p"))))
    return cells


def extract_spans(path: str | Path, doc_id: str, *, start_span: int = 0) -> list[Span]:
    """读一份 docx，按段落与表格行产出 Span（页码为 None，行内偏移单调）。"""
    p = Path(path)
    try:
        with zipfile.ZipFile(p) as zf:
            xml_bytes = zf.read(DOCUMENT_PART)
    except (OSError, KeyError, zipfile.BadZipFile):
        return []

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []

    body = root.find(_w("body"))
    if body is None:
        return []

    spans: list[Span] = []
    cursor = 0
    idx = 0

    def _emit(text: str, kind: str, bound: dict) -> None:
        nonlocal cursor, idx
        spans.append(
            Span(
                span_id=f"S-{start_span + idx:06d}",
                doc_id=doc_id,
                page=None,
                char_range=(cursor, cursor + len(text)),
                quoted_text=text,
                decimals_as_disclosed=decimals_of(text),
                span_kind=kind,
                bound_context=bound,
                extractor=ExtractorTier.DOCX.value,
            )
        )
        cursor += len(text)
        idx += 1

    for child in list(body):
        if child.tag == _w("p"):
            text = _para_text(child)
            if text.strip():
                _emit(text, SpanKind.PARAGRAPH.value, {})
        elif child.tag == _w("tbl"):
            for row in child.findall(_w("tr")):
                cells = _row_cells(row)
                text = _CELL_SEP.join(cells)
                if not text.strip():
                    continue
                row_label = cells[0].strip() if cells else ""
                _emit(text, SpanKind.TABLE_ROW.value, {"row_label": row_label})

    return spans


__all__ = ["extract_spans"]
