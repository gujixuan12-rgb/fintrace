"""步骤 ⑤：证据检索与页码定位。

目标：任何一条风险信号都必须能回溯到「哪个文件、第几页、哪句话」。
检索不到就返回空引用并在报告里写「无法定位原始证据」，
绝不编一个看起来合理的页码。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from ..models import EvidenceRef, SourceDocument


@dataclass
class Page:
    source_id: str
    file_name: str
    source_type: str
    publication_date: str
    page: int
    text: str


@dataclass
class EvidenceIndex:
    """轻量全文索引：mock 阶段用内存 dict，接真实 PDF 时换成 pymupdf 逐页抽取。"""

    docs: dict[str, SourceDocument] = field(default_factory=dict)
    pages: list[Page] = field(default_factory=list)

    @classmethod
    def build(cls, documents: Iterable[dict], sources: dict[str, SourceDocument]) -> "EvidenceIndex":
        idx = cls(docs=dict(sources))
        for d in documents:
            sd = sources.get(d["source_id"])
            if sd is None:
                continue
            for p in d.get("pages") or []:
                idx.pages.append(
                    Page(
                        source_id=sd.source_id,
                        file_name=sd.file_name,
                        source_type=sd.source_type,
                        publication_date=sd.publication_date,
                        page=int(p["page"]),
                        text=p.get("text", ""),
                    )
                )
        return idx

    # -- 检索 ---------------------------------------------------------------

    def search(self, needle: str, limit: int = 3) -> list[Page]:
        """按原文片段检索。needle 会被拆成关键词做 AND 匹配。"""
        kws = [k for k in re.split(r"[\s,，、]+", needle.strip()) if k]
        if not kws:
            return []
        hits = [p for p in self.pages if all(k in p.text for k in kws)]
        return hits[:limit]

    def find_value(self, metric: str, value: float, unit: str, tol: float = 0.005) -> list[Page]:
        """定位某个指标值的出处：同页必须同时出现指标名和（格式化后的）数值。"""
        cands = self.search(metric)
        out = []
        for p in cands:
            if self._page_mentions_number(p.text, value, unit, tol):
                out.append(p)
        return out

    @staticmethod
    def _page_mentions_number(text: str, value: float, unit: str, tol: float) -> bool:
        # 页面上数值可能带千分位或小数位不同，逐个数字尝试比对
        for m in re.finditer(r"-?\d[\d,]*\.?\d*", text):
            raw = m.group(0).replace(",", "")
            try:
                got = float(raw)
            except ValueError:
                continue
            if value == 0:
                if got == 0:
                    return True
                continue
            if abs(got - value) / abs(value) <= tol:
                return True
        return False

    # -- 组装引用 -----------------------------------------------------------

    def cite(self, pages: list[Page], quote_len: int = 80) -> list[EvidenceRef]:
        refs = []
        for p in pages:
            refs.append(
                EvidenceRef(
                    source_id=p.source_id,
                    file_name=p.file_name,
                    source_type=p.source_type,
                    publication_date=p.publication_date,
                    page=p.page,
                    quoted_text=p.text[:quote_len].replace("\n", " "),
                )
            )
        return refs
