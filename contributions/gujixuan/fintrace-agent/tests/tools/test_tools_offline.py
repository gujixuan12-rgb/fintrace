"""各工具离线行为测试（不触网）。

本文件全程把 ``urlopen`` / ``socket`` 换成炸弹——只要有任何一次网络调用就即时失败。
覆盖：t_parse / t_spec_extract / t_extract（规则回落）/ t_compute（舍入留痕）/
t_lookup / t_recall / t_knowledge / t_search_official（参数校验与未命中）/ t_review。
"""
from __future__ import annotations

import socket
import urllib.request
import zipfile
from pathlib import Path

import pytest

from fintrace_agent.contracts import Assertion, Span
from fintrace_agent.facts import FactStore
from fintrace_agent.knowledge.loader import KnowledgeBase
from fintrace_agent.paths import KNOWLEDGE_DB
from fintrace_agent.store.evidence import EvidenceLog
from fintrace_agent.tools import registry
from fintrace_agent.tools.registry import ToolContext, execute

#: 一份最小但结构完整的 docx 正文（段落 + 表格行）
DOCX_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    "<w:body>"
    "<w:p><w:r><w:t>营业收入 12,345.67 万元</w:t></w:r></w:p>"
    "<w:tbl><w:tr>"
    "<w:tc><w:p><w:r><w:t>现金分红金额</w:t></w:r></w:p></w:tc>"
    "<w:tc><w:p><w:r><w:t>1,234.56</w:t></w:r></w:p></w:tc>"
    "</w:tr></w:tbl>"
    "</w:body></w:document>"
)


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    """blobs / runs 重定向到临时目录。"""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("FINTRACE_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """整个文件都不许触网。"""
    def _boom(*args, **kwargs):  # pragma: no cover - 触网才会走到
        raise AssertionError("离线测试发生了网络调用！")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    return _boom


def _write_docx(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("word/document.xml", DOCX_XML)
    return path


# --------------------------------------------------------------------------- t_parse


def test_parse_docx(tmp_path):
    docx = _write_docx(tmp_path / "材料.docx")
    r = execute("t_parse", {"path": str(docx), "doc_id": "D1"}, ToolContext())
    assert r.ok is True
    assert r.result["format"] == "docx"
    assert r.result["extractor"] == "docx"
    kinds = {s["span_kind"] for s in r.result["spans"]}
    assert {"paragraph", "table_row"} <= kinds
    row = next(s for s in r.result["spans"] if s["span_kind"] == "table_row")
    assert row["bound_context"]["row_label"] == "现金分红金额"
    assert "1,234.56" in row["quoted_text"]


def test_parse_text(tmp_path):
    txt = tmp_path / "材料.txt"
    txt.write_text("第一段 营业收入 100 万元\n\n第二段 净利润 20 万元\n", encoding="utf-8")
    r = execute("t_parse", {"path": str(txt), "doc_id": "D2"}, ToolContext())
    assert r.ok is True
    assert r.result["format"] == "text"
    assert r.result["count"] == 2
    assert any("营业收入" in s["quoted_text"] for s in r.result["spans"])


def test_parse_unknown_format(tmp_path):
    blob = tmp_path / "材料.bin"
    blob.write_bytes(b"\x00\x01\x02\x03\xff\xfe" + "未知二进制".encode("utf-8"))
    r = execute("t_parse", {"path": str(blob), "doc_id": "D3"}, ToolContext())
    assert r.ok is False
    assert r.error == "unknown_material_format"


def test_parse_missing_file(tmp_path):
    r = execute("t_parse", {"path": str(tmp_path / "nope.pdf")}, ToolContext())
    assert r.ok is False
    assert r.error == "not_found"


def test_parse_requires_path():
    r = execute("t_parse", {}, ToolContext())
    assert r.ok is False
    assert r.error == "bad_args"


# ------------------------------------------------------------------- t_spec_extract


def _valid_spec() -> dict:
    return {
        "spec_id": "S-annual-revenue",
        "target": "D1",
        "fields": [
            {
                "name": "claim_text",
                "locate": {"kind": "regex_in_paragraph",
                           "pattern": r"营业收入\s*[\d,]+(?:\.\d+)?\s*万元"},
                "extract": {"kind": "text"},
            },
            {
                "name": "value",
                "locate": {"kind": "regex_in_paragraph",
                           "pattern": r"营业收入\s*([\d,]+(?:\.\d+)?)\s*万元"},
                "extract": {"kind": "group", "index": 1},
            },
        ],
    }


def test_spec_extract_valid():
    pages = [{"page": 1, "doc_id": "D1", "text": "公司实现营业收入 123,456.78 万元，同比增长。"}]
    r = execute("t_spec_extract", {"spec": _valid_spec(), "pages": pages}, ToolContext())
    assert r.ok is True
    assert r.result["spec_id"] == "S-annual-revenue"
    assert r.result["count"] >= 2
    value_span = next(s for s in r.result["spans"] if s["quoted_text"] == "123,456.78")
    assert value_span["decimals_as_disclosed"] == 2
    assert value_span["extractor"] == "spec"


def test_spec_extract_rejects_invalid_spec():
    bad = {"spec_id": "bad", "fields": [
        {"name": "value", "locate": {"kind": "exec_shell"}, "extract": {"kind": "text"}},
    ]}
    r = execute("t_spec_extract", {"spec": bad, "pages": [{"page": 1, "text": "x"}]}, ToolContext())
    assert r.ok is False
    assert r.error == "invalid_spec"


def test_spec_extract_requires_pages():
    r = execute("t_spec_extract", {"spec": _valid_spec(), "pages": []}, ToolContext())
    assert r.ok is False
    assert r.error == "bad_args"


# ----------------------------------------------------------------------- t_extract


def test_extract_rule_fallback_when_llm_not_allowed(monkeypatch):
    # 就算有人误调模型也应即时失败——证明规则回落路径没碰模型
    from fintrace_engine import llm as _llm

    def _should_not_call(*a, **k):  # pragma: no cover - 被调用即失败
        raise AssertionError("不应调用大模型")

    monkeypatch.setattr(_llm, "chat", _should_not_call)

    text = "公司实现营业收入 123,456.78 万元。净利润 9,876.54 万元，同比增长。"
    r = execute("t_extract", {"text": text, "doc_id": "D1", "allow_llm": False}, ToolContext())
    assert r.ok is True
    assert r.result["extracted_by"]["how"] == "rule"
    assert r.result["count"] >= 2
    units = {c["unit"] for c in r.result["claims"]}
    assert "万元" in units
    assert all(c["quoted_text"] for c in r.result["claims"])


def test_extract_rule_fallback_when_llm_disabled_in_config():
    text = "总股本 81,000 万股。"
    ctx = ToolContext(config={"llm": {"enabled": False}})
    r = execute("t_extract", {"text": text}, ctx)
    assert r.ok is True
    assert r.result["extracted_by"]["how"] == "rule"


def test_extract_requires_text():
    r = execute("t_extract", {}, ToolContext())
    assert r.ok is False
    assert r.error == "bad_args"


# ----------------------------------------------------------------------- t_compute


def test_compute_explicit_formula_records_rounding():
    args = {
        "formula": {
            "formula_id": "expected_dividend_yuan",
            "metric_id": "cash_dividend",
            "expression": "shares * per_ten / 10",
            "inputs": ["shares", "per_ten"],
            "rounding": {"scale": 2, "mode": "ROUND_HALF_UP"},
            "rule_version": "2",
        },
        "inputs": {"shares": "810000000", "per_ten": "0.155"},
    }
    r = execute("t_compute", args, ToolContext())
    assert r.ok is True
    res = r.result
    assert res["rounding_step"] == "calc_output"
    assert res["rules_version"] == "2"
    assert res["rule_id"] == "expected_dividend_yuan"
    assert res["rounding"] == {"scale": 2, "mode": "ROUND_HALF_UP"}
    # 舍入前全精度值与舍入后值都要留，且写法不同
    assert res["value_unrounded"] == "12555000.000"
    assert res["value"] == "12555000.00"


def test_compute_keeps_full_precision_before_rounding():
    # a/b 是无限小数：舍入前必须保留全精度，舍入后按 scale 收敛
    args = {
        "formula": {"formula_id": "eps_like", "expression": "a / b",
                    "rounding": {"scale": 4, "mode": "ROUND_HALF_UP"},
                    "rule_version": "9"},
        "inputs": {"a": "10", "b": "3"},
    }
    r = execute("t_compute", args, ToolContext())
    assert r.ok is True
    res = r.result
    assert res["value"] == "3.3333"
    assert res["value_unrounded"].startswith("3.3333333")
    assert len(res["value_unrounded"]) > len(res["value"])
    assert res["rounding_step"] == "calc_output"
    assert res["rules_version"] == "9"


def test_compute_from_knowledge_base():
    kb = KnowledgeBase(KNOWLEDGE_DB)
    try:
        r = execute("t_compute",
                    {"metric_id": "cash_dividend",
                     "inputs": {"total_shares": 810000000, "dividend_per_ten": "0.155"}},
                    ToolContext(kb=kb))
    finally:
        kb.close()
    assert r.ok is True
    res = r.result
    assert res["rounding_step"] == "calc_output"
    assert res["rules_version"] == "1"
    assert res["rule_id"] == "expected_dividend_yuan"
    assert res["unit"] == "元"
    assert res["value"] == "12555000.00"
    assert "value_unrounded" in res


def test_compute_rejects_unknown_input_name():
    args = {"formula": {"formula_id": "x", "expression": "a * b"}, "inputs": {"a": 1}}
    r = execute("t_compute", args, ToolContext())
    assert r.ok is False
    assert r.error == "bad_args"


def test_compute_rejects_code_like_expression():
    args = {"formula": {"formula_id": "x", "expression": "__import__('os').system('ls')"},
            "inputs": {}}
    r = execute("t_compute", args, ToolContext())
    assert r.ok is False
    assert r.error == "bad_args"


def test_compute_requires_formula():
    r = execute("t_compute", {"inputs": {}}, ToolContext())
    assert r.ok is False
    assert r.error == "bad_args"


# ------------------------------------------------------------------------ t_lookup


def _fact_store() -> FactStore:
    store = FactStore(Path(":memory:"))
    store.put_assertion(Assertion(
        assertion_id="A1", kind="metric", subject="启明信息", period="2023A",
        metric="营业收入", value="100", evidence_status="attested", semantic_status="bound",
        source_spans=[], extracted_by={}))
    store.put_assertion(Assertion(
        assertion_id="A2", kind="metric", subject="启明信息", period="2023A",
        metric="净利润", value="20", evidence_status="candidate", semantic_status="unbound",
        source_spans=[], extracted_by={}))
    return store


def test_lookup_only_facts():
    store = _fact_store()
    try:
        r = execute("t_lookup", {"subject": "启明信息", "only_facts": True},
                    ToolContext(facts=store))
    finally:
        store.close()
    assert r.ok is True
    assert r.result["count"] == 1
    assert r.result["assertions"][0]["assertion_id"] == "A1"


def test_lookup_includes_candidates_when_asked():
    store = _fact_store()
    try:
        r = execute("t_lookup", {"subject": "启明信息", "only_facts": False},
                    ToolContext(facts=store))
    finally:
        store.close()
    assert r.ok is True
    assert r.result["count"] == 2


def test_lookup_requires_filter():
    store = _fact_store()
    try:
        r = execute("t_lookup", {}, ToolContext(facts=store))
    finally:
        store.close()
    assert r.ok is False
    assert r.error == "bad_args"


def test_lookup_without_store():
    r = execute("t_lookup", {"subject": "x"}, ToolContext())
    assert r.ok is False
    assert r.error == "not_configured"


# ------------------------------------------------------------------------ t_recall


def _evidence() -> EvidenceLog:
    ev = EvidenceLog("FT-TEST-RECALL")
    ev.add_spans([
        Span(span_id="S-000001", doc_id="D1", page=1, quoted_text="营业收入 100 万元"),
        Span(span_id="S-000002", doc_id="D1", page=2, quoted_text="净利润 20 万元"),
    ])
    return ev


def test_recall_by_keyword_returns_raw_text():
    ev = _evidence()
    r = execute("t_recall", {"keywords": ["营业收入"]}, ToolContext(evidence=ev))
    assert r.ok is True
    assert r.result["count"] == 1
    assert r.result["spans"][0]["quoted_text"] == "营业收入 100 万元"


def test_recall_by_span_ids():
    ev = _evidence()
    r = execute("t_recall", {"span_ids": ["S-000002"]}, ToolContext(evidence=ev))
    assert r.ok is True
    assert r.result["spans"][0]["quoted_text"] == "净利润 20 万元"


def test_recall_requires_a_filter():
    ev = _evidence()
    r = execute("t_recall", {}, ToolContext(evidence=ev))
    assert r.ok is False
    assert r.error == "bad_args"


def test_recall_without_evidence():
    r = execute("t_recall", {"keywords": ["x"]}, ToolContext())
    assert r.ok is False
    assert r.error == "not_configured"


# --------------------------------------------------------------------- t_knowledge


def test_knowledge_resolve_metric():
    kb = KnowledgeBase(KNOWLEDGE_DB)
    try:
        r = execute("t_knowledge", {"op": "resolve_metric", "name": "归母净利润"},
                    ToolContext(kb=kb))
    finally:
        kb.close()
    assert r.ok is True
    assert r.result["hit"] is True
    assert r.result["value"]["metric_id"] == "net_profit_parent"


def test_knowledge_precision_for_unit():
    kb = KnowledgeBase(KNOWLEDGE_DB)
    try:
        r = execute("t_knowledge", {"op": "precision_for", "metric_id": "revenue", "unit": "元"},
                    ToolContext(kb=kb))
    finally:
        kb.close()
    assert r.ok is True
    assert r.result["value"]["precision"] == 2


def test_knowledge_rejects_unknown_op():
    r = execute("t_knowledge", {"op": "drop_table"}, ToolContext())
    assert r.ok is False
    assert r.error == "bad_args"


def test_knowledge_without_kb():
    r = execute("t_knowledge", {"op": "all_metrics"}, ToolContext())
    assert r.ok is False
    assert r.error == "not_configured"


# ---------------------------------------------------------------- t_search_official


def test_search_official_param_validation():
    r = execute("t_search_official", {}, ToolContext())
    assert r.ok is False
    assert r.error == "bad_args"
    r2 = execute("t_search_official", {"code": "002232", "limit": 0}, ToolContext())
    assert r2.ok is False
    assert r2.error == "bad_args"
    r3 = execute("t_search_official", {"code": "002232", "mode": "wat"}, ToolContext())
    assert r3.ok is False
    assert r3.error == "bad_args"


def test_search_official_local_miss(tmp_path):
    r = execute("t_search_official",
                {"mode": "local", "code": "002232", "cache_dir": str(tmp_path)},
                ToolContext())
    assert r.ok is True
    assert r.result["count"] == 0
    assert r.result["documents"] == []
    assert r.result["note"] == "未命中本地清单"


def test_search_official_local_hit(tmp_path):
    # 预置一份本地 manifest（不触网），local 模式应能命中
    import json

    (tmp_path / "manifest.json").write_text(json.dumps({
        "1001": {"code": "002232", "name": "启明信息", "org_id": "9900004567",
                 "announcement_id": "1001", "title": "2023年年度报告",
                 "published_at": "2024-03-15", "url": "http://x/a.PDF", "size_kb": 2048,
                 "category": "category_ndbg_szsh", "path": "", "sha256": "ab", "pages": 120,
                 "fetched_at": "2024-03-16T10:00:00"},
    }, ensure_ascii=False), encoding="utf-8")

    r = execute("t_search_official",
                {"mode": "local", "code": "002232", "category": "ndbg", "cache_dir": str(tmp_path)},
                ToolContext())
    assert r.ok is True
    assert r.result["count"] == 1
    doc = r.result["documents"][0]
    assert doc["doc_id"] == "CNINFO-1001"
    assert doc["source_id"] == "002232"
    assert doc["acquisition"] == "cninfo"


# ------------------------------------------------------------------- 默认关闭动作


def test_review_and_author_are_disabled():
    for name in ("t_review", "t_author_tool"):
        r = execute(name, {}, ToolContext())
        assert r.ok is False
        assert r.error == "disabled"
