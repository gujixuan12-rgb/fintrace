"""store/evidence.py 测试：只追加、重读一致、id 唯一、index 只给 id。"""
from __future__ import annotations

from fintrace_agent.contracts import Assertion, Span
from fintrace_agent.store import blobs
from fintrace_agent.store.evidence import EvidenceLog

RUN_ID = "FT-20261010-0000abcd"


def _span(sid: str, text: str = "原文片段") -> Span:
    return Span(span_id=sid, doc_id="doc-1", page=3, quoted_text=text)


def _assertion(aid: str) -> Assertion:
    return Assertion(assertion_id=aid, kind="financial_value", metric="营业收入",
                     value="4085484.55", run_id=RUN_ID)


def test_add_spans_returns_ids_in_order():
    log = EvidenceLog(RUN_ID)
    ids = log.add_spans([_span("s1"), _span("s2")])
    assert ids == ["s1", "s2"]
    assert [s.span_id for s in log.spans()] == ["s1", "s2"]


def test_add_assertions_and_lookup():
    log = EvidenceLog(RUN_ID)
    log.add_assertions([_assertion("a1")])
    a = log.assertion("a1")
    assert a is not None
    assert a.metric == "营业收入"
    assert log.assertion("missing") is None
    assert log.span("missing") is None


def test_span_lookup_roundtrip():
    log = EvidenceLog(RUN_ID)
    log.add_spans([_span("s1", "营业收入 4,085,484.55")])
    s = log.span("s1")
    assert s is not None
    assert s.quoted_text == "营业收入 4,085,484.55"
    assert s.page == 3


def test_ids_unique_no_duplicate_append(fintrace_home):
    log = EvidenceLog(RUN_ID)
    log.add_spans([_span("s1")])
    log.add_spans([_span("s1"), _span("s2")])   # s1 重复
    assert [s.span_id for s in log.spans()] == ["s1", "s2"]
    # 磁盘上也只有两条记录
    lines = [ln for ln in log.path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 2


def test_append_only_is_prefix(fintrace_home):
    log = EvidenceLog(RUN_ID)
    log.add_spans([_span("s1")])
    first = log.path.read_bytes()
    log.add_spans([_span("s2")])
    second = log.path.read_bytes()
    assert second.startswith(first)          # 追加：旧内容一字不改
    assert len(second) > len(first)


def test_reopen_process_consistent(fintrace_home):
    log = EvidenceLog(RUN_ID)
    log.add_spans([_span("s1", "甲"), _span("s2", "乙")])
    log.add_assertions([_assertion("a1")])

    # 模拟重开进程：新实例从磁盘回读
    again = EvidenceLog(RUN_ID)
    assert [s.to_dict() for s in again.spans()] == [s.to_dict() for s in log.spans()]
    assert [a.to_dict() for a in again.assertions()] == [a.to_dict() for a in log.assertions()]
    assert again.index() == log.index()


def test_reopen_tolerates_truncated_line(fintrace_home):
    log = EvidenceLog(RUN_ID)
    log.add_spans([_span("s1")])
    # 模拟断电留下的半行
    with log.path.open("a", encoding="utf-8") as f:
        f.write('{"type": "span", "data": {"span_id": "s2"')

    again = EvidenceLog(RUN_ID)
    assert [s.span_id for s in again.spans()] == ["s1"]


def test_index_gives_ids_not_text(fintrace_home):
    log = EvidenceLog(RUN_ID)
    secret = "营业收入 4,085,484.55"
    log.add_spans([_span("s1", secret)])
    log.add_assertions([_assertion("a1")])

    idx = log.index()
    assert idx == ["s1", "a1"]
    assert secret not in idx
    assert all(text not in idx for text in ("营业收入 4,085,484.55",))


def test_add_spans_requires_id():
    import pytest

    log = EvidenceLog(RUN_ID)
    with pytest.raises(ValueError):
        log.add_spans([Span(span_id="", doc_id="d", page=1)])


def test_blob_put_delegates_to_blobs():
    log = EvidenceLog(RUN_ID)
    sha = log.blob_put("一段很长的正文")
    assert sha == blobs.put_text("一段很长的正文")
    assert blobs.get_text(sha) == "一段很长的正文"
