"""精度判定与文本桥接（v2.2 §5.3、§6.4）：decimals_of 关键反例 + text_to_spans。"""
from __future__ import annotations

import pytest

from fintrace_agent.contracts import SpanKind
from fintrace_agent.parsers.bridge import decimals_of, text_to_spans


@pytest.mark.parametrize("raw,expected", [
    ("408,548.46", 2),          # 千分位 + 2 位小数（必测）
    ("4,085,484.55", 2),
    ("0.155", 3),               # 必测：三位小数不许被截成两位
    ("12", 0),                  # 必测：整数 = 0 位
    ("0", 0),
    ("408,548", 0),
    ("408548.46", 2),
    ("  12.30  ", 2),           # 首尾空白无妨
    ("-12.5", 1),               # 负号不影响小数位
])
def test_decimals_of_accepts_pure_numbers(raw, expected):
    assert decimals_of(raw) == expected


@pytest.mark.parametrize("raw", [
    "约 1,000 万元",            # 必测：带「约」和单位 → None
    "1,000 万元",               # 带单位 → None
    "约 1000",                  # 带「约」→ None
    "408,548.46 元",
    "0.155元",
    "1,000亿",
    "12个月",
    "元",
    "",
    "  ",
    "1,00",                     # 千分位必须严格 3 位
    "12.",                      # 尾随小数点，缺小数位 → None（宁可 None）
    ".5",                       # 无整数部分 → None
    "1.2.3",
    "408,548.46,",              # 尾随逗号
    None,
])
def test_decimals_of_rejects_non_pure_numbers(raw):
    assert decimals_of(raw) is None


def test_text_to_spans_basic():
    pages = [
        {"page": 1, "text": "第一段内容。\n\n第二段含数字 408,548.46 元。"},
        {"page": 2, "text": "408,548.46"},
    ]
    spans = text_to_spans(pages, "D-1")
    assert [s.quoted_text for s in spans] == ["第一段内容。", "第二段含数字 408,548.46 元。", "408,548.46"]
    assert [s.page for s in spans] == [1, 1, 2]
    assert all(s.doc_id == "D-1" for s in spans)
    assert all(s.span_kind == SpanKind.PARAGRAPH.value for s in spans)
    # 只有纯数字那段有 decimals
    assert [s.decimals_as_disclosed for s in spans] == [None, None, 2]


def test_text_to_spans_char_range_monotonic():
    pages = [{"page": 1, "text": "A。\n\nB。"}, {"page": 2, "text": "C。"}]
    spans = text_to_spans(pages, "D-1")
    prev = -1
    for s in spans:
        lo, hi = s.char_range
        assert lo >= prev
        prev = hi


def test_text_to_spans_respects_kind_and_extractor():
    spans = text_to_spans([{"page": 3, "text": "x"}], "D-1",
                          span_kind=SpanKind.TABLE_ROW.value, extractor="spec")
    assert spans[0].span_kind == SpanKind.TABLE_ROW.value
    assert spans[0].extractor == "spec"


def test_text_to_spans_skips_blank_pages():
    spans = text_to_spans([{"page": 1, "text": "\n\n   \n"}, {"page": 2, "text": "有内容"}], "D-1")
    assert len(spans) == 1 and spans[0].page == 2
