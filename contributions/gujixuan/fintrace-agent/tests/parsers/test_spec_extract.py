"""声明式抽取规格（v2.2 §5.4）：validate_spec 拒绝非法规格；run_spec 抽值并保留 raw。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from fintrace_agent.contracts import ExtractorTier, SpanKind
from fintrace_agent.parsers.spec_extract import load_spec, run_spec, validate_spec

VALID_SPEC = {
    "spec_id": "SP-002",
    "target": "研报草稿.txt",
    "fields": [
        {"name": "claim_text",
         "locate": {"kind": "paragraph", "contains": ["预计", "营业收入"]},
         "extract": {"kind": "text"}},
        {"name": "value",
         "locate": {"kind": "regex_in_paragraph", "pattern": "收入([0-9,.]+)(亿元|万元)"},
         "extract": {"kind": "group", "index": 1}},
    ],
    "unit_hint": "万元",
    "period_hint": "2024E",
}

PAGES = [{"page": 1, "text": "其他内容。\n\n预计2024年营业收入1,234.56万元。\n\n结尾。"}]


# ---------------------------------------------------------------------------
# 合法规格：先校验通过，再抽值
# ---------------------------------------------------------------------------


def test_valid_spec_passes_validation():
    assert validate_spec(VALID_SPEC) == []


def test_run_spec_extracts_value_with_raw_and_decimals():
    spans = run_spec(VALID_SPEC, PAGES)
    by_id = {s.quoted_text: s for s in spans}
    # claim_text 段落
    claim = [s for s in spans if s.quoted_text.startswith("预计")][0]
    assert claim.span_kind == SpanKind.PARAGRAPH.value
    # value 组抽取：原文原样保留（带千分位），精度正确
    val = by_id["1,234.56"]
    assert val.decimals_as_disclosed == 2
    assert val.extractor == ExtractorTier.SPEC.value
    assert val.spec_id == "SP-002"
    assert val.page == 1
    assert val.bound_context.get("unit") == "万元"
    assert val.bound_context.get("period") == "2024E"


def test_run_spec_group_index_two():
    spec = json.loads(json.dumps(VALID_SPEC))
    spec["fields"][1]["extract"]["index"] = 2
    spans = run_spec(spec, PAGES)
    units = [s.quoted_text for s in spans if s.spec_id == "SP-002"]
    assert "万元" in units


def test_run_spec_char_range_slices_to_quoted_text():
    """char_range 必须能精确切回 quoted_text（页码 + 区间就是定位口径）。"""
    text = PAGES[0]["text"]
    spans = run_spec(VALID_SPEC, PAGES)
    assert spans
    for s in spans:
        lo, hi = s.char_range
        assert s.page == 1
        assert text[lo:hi] == s.quoted_text, f"定位对不上：{s.quoted_text!r}"


def test_run_spec_table_row_locate_sets_kind_and_label():
    spec = {
        "spec_id": "SP-TBL",
        "target": "x.txt",
        "fields": [{"name": "row_label",
                    "locate": {"kind": "table_row", "contains": ["分红"]},
                    "extract": {"kind": "text"}}],
    }
    pages = [{"page": 5, "text": "现金分红 | 408,548.46 | 元\n其他行"}]
    spans = run_spec(spec, pages)
    assert len(spans) == 1
    assert spans[0].span_kind == SpanKind.TABLE_ROW.value
    assert spans[0].bound_context["row_label"] == "现金分红"


# ---------------------------------------------------------------------------
# 非法规格：逐条拒绝，且 run_spec 不得执行
# ---------------------------------------------------------------------------


def _errs(mutate) -> list[str]:
    spec = json.loads(json.dumps(VALID_SPEC))
    mutate(spec)
    return validate_spec(spec)


def test_reject_unknown_locate_kind():
    def m(s):
        s["fields"][0]["locate"]["kind"] = "shell"
    errs = _errs(m)
    assert errs


def test_reject_unknown_extract_kind():
    def m(s):
        s["fields"][0]["extract"]["kind"] = "eval_group"
    errs = _errs(m)
    assert errs


def test_reject_field_name_not_in_whitelist():
    def m(s):
        s["fields"][0]["name"] = "earnings"
    errs = _errs(m)
    assert any("白名单" in e for e in errs)


def test_reject_dangerous_word_in_pattern():
    def m(s):
        s["fields"][1]["locate"]["pattern"] = "import([0-9]+)"
    errs = _errs(m)
    assert any("import" in e for e in errs)


def test_reject_dangerous_word_open_anywhere():
    def m(s):
        s["target"] = "open_gate.txt"
    errs = _errs(m)
    assert any("open" in e for e in errs)


def test_reject_dunder_trace():
    def m(s):
        s["fields"][1]["locate"]["pattern"] = "收入(__import__)([0-9]+)"
    errs = _errs(m)
    assert errs


def test_reject_group_index_not_int():
    def m(s):
        s["fields"][1]["extract"]["index"] = "1"
    errs = _errs(m)
    assert any("index" in e for e in errs)


def test_reject_group_with_non_regex_locate():
    def m(s):
        s["fields"][0]["extract"] = {"kind": "group", "index": 0}
    errs = _errs(m)
    assert errs


def test_reject_invalid_regex():
    def m(s):
        s["fields"][1]["locate"]["pattern"] = "收入([0-9"
    errs = _errs(m)
    assert any("正则" in e for e in errs)


def test_reject_unknown_top_level_key():
    def m(s):
        s["payload"] = "x"
    errs = _errs(m)
    assert any("payload" in e for e in errs)


def test_reject_missing_spec_id():
    def m(s):
        del s["spec_id"]
    errs = _errs(m)
    assert any("spec_id" in e for e in errs)


def test_run_spec_refuses_invalid_spec():
    bad = json.loads(json.dumps(VALID_SPEC))
    bad["fields"][0]["locate"]["kind"] = "shell"
    with pytest.raises(ValueError):
        run_spec(bad, PAGES)


# ---------------------------------------------------------------------------
# load_spec
# ---------------------------------------------------------------------------


def test_load_spec_roundtrip(tmp_path: Path):
    p = tmp_path / "spec.json"
    p.write_text(json.dumps(VALID_SPEC, ensure_ascii=False), encoding="utf-8")
    loaded = load_spec(p)
    assert loaded == VALID_SPEC
    assert validate_spec(loaded) == []
