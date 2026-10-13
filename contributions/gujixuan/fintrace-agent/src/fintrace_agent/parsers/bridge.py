"""Span 桥接与精度判定（v2.2 §5.3、§6.4）。

``decimals_of`` 是**精度保真**的第一道关口（模块 E 的 W6 依赖它）：只认纯数字/千分位数字串，
带单位、带中文、带「约」一律返回 ``None``——**宁可 None，不许截断**。
"""
from __future__ import annotations

import re

from ..contracts import Span, SpanKind

#: 纯数字（可带千分位，可带正负号）才认；小数位=披露几位就几位。
#: 不匹配任何带单位/中文/「约」的串 → None。
_NUM_RE = re.compile(r"^\s*[-+]?\s*(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?\s*$")

#: 段落之间的空白行分隔（一个空行切一段）
_PARA_SPLIT = re.compile(r"\n\s*\n")


def decimals_of(raw: str | None) -> int | None:
    """披露了几位小数；只认纯数字/千分位数字串，其余返回 ``None``。

    例：``"408,548.46"`` → 2；``"0.155"`` → 3；``"12"`` → 0；``"约 1,000 万元"`` → None。
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        raw = str(raw)
    m = _NUM_RE.match(raw)
    if m is None:
        return None
    frac = m.group(2)
    return len(frac) if frac else 0


def text_to_spans(pages: list[dict], doc_id: str, *, span_kind: str = "paragraph",
                  extractor: str = "builtin") -> list[Span]:
    """把「逐页文本」桥接成 Span 列表。

    ``pages`` 形如 ``[{"page": int, "text": str}]``（页码 1 起，与
    ``fintrace_engine.parsers.pdf_pages`` 口径一致）。每页按空行切成段落，非空段落各出一个 Span。

    ``char_range`` 用跨页累计的字符偏移（**单调不减**，可当文档级定位用）；
    ``decimals_as_disclosed`` 只在该段整体就是纯数字时才有值。
    """
    spans: list[Span] = []
    cursor = 0
    idx = 0
    for pg in pages:
        page_no = pg.get("page")
        text = pg.get("text") or ""
        for para in _PARA_SPLIT.split(text):
            if not para.strip():
                continue
            spans.append(
                Span(
                    span_id=f"S-{idx:06d}",
                    doc_id=doc_id,
                    page=page_no,
                    char_range=(cursor, cursor + len(para)),
                    quoted_text=para,
                    decimals_as_disclosed=decimals_of(para),
                    span_kind=span_kind or SpanKind.PARAGRAPH.value,
                    extractor=extractor,
                )
            )
            cursor += len(para)
            idx += 1
    return spans


__all__ = ["decimals_of", "text_to_spans"]
