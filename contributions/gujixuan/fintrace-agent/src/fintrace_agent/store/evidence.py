"""证据日志（只追加）。

落盘于 ``runs/<run_id>/evidence.jsonl``，每行一条记录：

    {"type": "span"|"assertion", "data": {...}}

不变量：
- **只追加**：绝不覆盖或删除历史行（原文留档是红线）；
- ``span_id`` / ``assertion_id`` 全局唯一：同一 id 重复登记不重复落盘，返回同 id；
- 进程重开后读回与内存一致；
- ``index()`` 只给 id，不给正文。

大文本不塞进日志，走 ``blob_put`` 落到内容寻址库，日志里只留 hash。
"""
from __future__ import annotations

from ..contracts import Assertion, Span
from . import blobs
from .runs import append_jsonl, read_jsonl, run_dir

_TYPE_SPAN = "span"
_TYPE_ASSERTION = "assertion"


class EvidenceLog:
    """单个 run 的证据日志。构造即确保运行目录存在，并从磁盘回读已有记录。"""

    def __init__(self, run_id: str):
        self.run_id = run_id
        self.dir = run_dir(run_id, create=True)
        self.path = self.dir / "evidence.jsonl"
        self._spans: dict[str, Span] = {}
        self._assertions: dict[str, Assertion] = {}
        self._order: list[str] = []          # 插入顺序（span_id / assertion_id 混排）
        self._load()

    # -- 内部：从磁盘重建内存索引 ------------------------------------------

    def _load(self) -> None:
        for rec in read_jsonl(self.path):
            rtype = rec.get("type")
            data = rec.get("data") or {}
            if rtype == _TYPE_SPAN and data.get("span_id"):
                sid = data["span_id"]
                if sid not in self._spans:
                    self._spans[sid] = Span.from_dict(data)
                    self._order.append(sid)
            elif rtype == _TYPE_ASSERTION and data.get("assertion_id"):
                aid = data["assertion_id"]
                if aid not in self._assertions:
                    self._assertions[aid] = Assertion.from_dict(data)
                    self._order.append(aid)

    # -- 追加 --------------------------------------------------------------

    def add_spans(self, spans: list[Span]) -> list[str]:
        """追加 Span，返回 span_id 列表（按入参顺序）。同 id 幂等，不重复落盘。"""
        out: list[str] = []
        for span in spans:
            sid = (span.span_id or "").strip()
            if not sid:
                raise ValueError("span_id 不能为空")
            out.append(sid)
            if sid in self._spans:
                continue
            append_jsonl(self.path, {"type": _TYPE_SPAN, "data": span.to_dict()})
            self._spans[sid] = span
            self._order.append(sid)
        return out

    def add_assertions(self, items: list[Assertion]) -> list[str]:
        """追加 Assertion，返回 assertion_id 列表（按入参顺序）。同 id 幂等。"""
        out: list[str] = []
        for item in items:
            aid = (item.assertion_id or "").strip()
            if not aid:
                raise ValueError("assertion_id 不能为空")
            out.append(aid)
            if aid in self._assertions:
                continue
            append_jsonl(self.path, {"type": _TYPE_ASSERTION, "data": item.to_dict()})
            self._assertions[aid] = item
            self._order.append(aid)
        return out

    # -- 读取 --------------------------------------------------------------

    def spans(self) -> list[Span]:
        """按插入顺序返回全部 Span。"""
        return [self._spans[i] for i in self._order if i in self._spans]

    def assertions(self) -> list[Assertion]:
        """按插入顺序返回全部 Assertion。"""
        return [self._assertions[i] for i in self._order if i in self._assertions]

    def span(self, span_id: str) -> Span | None:
        return self._spans.get((span_id or "").strip())

    def assertion(self, assertion_id: str) -> Assertion | None:
        return self._assertions.get((assertion_id or "").strip())

    def index(self) -> list[str]:
        """证据索引：只给 id（span_id / assertion_id），不给正文。"""
        return list(self._order)

    # -- 大文本 ------------------------------------------------------------

    def blob_put(self, text: str) -> str:
        """大文本走内容寻址库，返回 sha256；日志里只应留这个 hash。"""
        return blobs.put_text(text)
