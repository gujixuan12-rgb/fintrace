"""步骤 ①：文件识别。

从文件名 + 首页文本判断材料类型与发布日期。真实场景下这一步由 pymupdf
读首页完成；mock 阶段直接读入参里给定的字段，保证接口一致。
"""
from __future__ import annotations

import re
from datetime import date

from ..models import SOURCE_TYPES, SourceDocument

#: 文件名 → 来源类型的判定规则（顺序敏感，先具体后宽泛）。
_FILENAME_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"问询函|关注函"), "inquiry_letter_reply"),
    (re.compile(r"更正公告|差错更正|更正及定期报告"), "correction_announcement"),
    (re.compile(r"审计报告"), "audit_report"),
    (re.compile(r"年度报告|年报(?!问询)"), "original_annual_report"),
    (re.compile(r"半年度报告|季度报告|半年报|季报"), "original_interim_report"),
    (re.compile(r"公告"), "announcement"),
    (re.compile(r"研报|研究报告|深度报告|评级报告"), "research_report"),
]

#: 从正文里抓「2020年年度报告」这类日期线索，作为 publication_date 的兜底。
_DATE_IN_TEXT = re.compile(r"(20\d{2})\s*年\s*(\d{1,2})?\s*月?\s*(\d{1,2})?\s*日?")


def classify(file_name: str) -> str:
    """按文件名判定来源类型；判不出返回 announcement 并在调用方标记人工确认。"""
    for rx, kind in _FILENAME_RULES:
        if rx.search(file_name):
            return kind
    return "announcement"


def guess_publication_date(file_name: str, first_page_text: str = "") -> str | None:
    """优先用文件名里的 8 位日期，其次取首页最早的完整日期。"""
    m = re.search(r"(20\d{2})[-_.]?(\d{2})[-_.]?(\d{2})", file_name)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    for mm in _DATE_IN_TEXT.finditer(first_page_text or ""):
        y, mo, d = mm.group(1), mm.group(2), mm.group(3)
        if mo and d:
            try:
                return date(int(y), int(mo), int(d)).isoformat()
            except ValueError:
                continue
    return None


def recognize(doc: dict) -> tuple[SourceDocument, list[str]]:
    """把一条原始文档 dict 规范成 SourceDocument。

    返回 (SourceDocument, 警告列表)。警告不会阻塞，但必须出现在报告里。
    """
    warnings: list[str] = []
    file_name = doc["file_name"]
    source_type = doc.get("source_type") or classify(file_name)
    if source_type not in SOURCE_TYPES:
        warnings.append(f"{file_name}: 未知来源类型 {source_type!r}，按 announcement 处理")
        source_type = "announcement"

    pub = doc.get("publication_date")
    if not pub:
        first = (doc.get("pages") or [{}])[0].get("text", "")
        pub = guess_publication_date(file_name, first)
        if pub:
            warnings.append(f"{file_name}: 发布日期由正文推断为 {pub}，建议人工复核")
        else:
            warnings.append(f"{file_name}: 无法确定发布日期，该材料不参与预警计算")

    return (
        SourceDocument(
            source_id=doc["source_id"],
            file_name=file_name,
            source_type=source_type,
            publication_date=pub or "",
            path=doc.get("path", ""),
            page_count=len(doc.get("pages") or []) or None,
        ),
        warnings,
    )


def recognize_all(docs: list[dict]) -> tuple[list[SourceDocument], list[str]]:
    out, warns = [], []
    for d in docs:
        sd, w = recognize(d)
        out.append(sd)
        warns.extend(w)
    return out, warns
