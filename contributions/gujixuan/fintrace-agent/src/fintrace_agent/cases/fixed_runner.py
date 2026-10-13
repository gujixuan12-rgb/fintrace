"""固定链路（fixed 模式）：直接调内核的草稿核查，翻成契约对象。

fixed 模式是**基线**（v2.2 §4.2）：评测与录屏兜底走它，口径与团队仓库一致；
自主回路（agent 模式）跑不动时可以回落到这里，不至于交不出东西。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from fintrace_agent.cases.adapter import CaseInput, documents_of
from fintrace_agent.contracts import Finding, Publish, Verdict


@dataclass
class FixedResult:
    case_id: str
    findings: list[Finding] = field(default_factory=list)
    raw: object = None                      # 内核原始结果，供评测复用口径
    warnings: list[str] = field(default_factory=list)
    run_log_id: str = ""
    stages: dict = field(default_factory=dict)

    def counts(self) -> dict:
        c = {v.value: 0 for v in Verdict}
        for f in self.findings:
            c[f.verdict] = c.get(f.verdict, 0) + 1
        return c


def _to_finding(row: dict, i: int) -> Finding:
    v = row.get("verdict") or {}
    label = str(v.get("label") or Verdict.INSUFFICIENT.value)
    evidence = list(v.get("evidence") or [])
    pages = [int(e["page"]) for e in evidence if e.get("page")]
    return Finding(
        finding_id=f"F-{i + 1:03d}",
        claim_id=str(row.get("claim_id") or ""),
        verdict=label,
        error_type=str(v.get("error_type") or ""),
        basis=str(v.get("basis") or ""),
        suggested_fix=str(v.get("suggested_fix") or ""),
        evidence=evidence,
        citation_pages=pages,
        correction={},
        audit={"mode": "fixed", "confidence": v.get("confidence"),
               "unable_reason": v.get("unable_reason"),
               "missing_input": v.get("missing_input"),
               "decomposed_by": row.get("decomposed_by")},
        publish=Publish.MANUAL_REVIEW.value,      # 是否发布由 V 门决定，fixed 模式不越权
    )


def run_fixed(case: CaseInput, *, root: Path | None = None, use_llm: bool = False) -> FixedResult:
    from fintrace_engine import draft_review as dr

    res = dr.run_draft_case(case.raw, use_llm=use_llm)
    out = FixedResult(
        case_id=case.case_id,
        raw=res,
        warnings=list(getattr(res, "warnings", []) or []),
        run_log_id=str(getattr(res, "run_log_id", "") or ""),
        stages=dict(getattr(res, "stages", {}) or {}),
    )
    for i, row in enumerate(res.claims):
        out.findings.append(_to_finding(row, i))
    return out


def page_texts_reader(case: CaseInput, *, root: Path | None = None):
    """给 V 门用的原件回查函数：(file_name, page) -> 该页正文（读不到返回 None）。

    这是「不信输出、只信原件」那一步的实现：V 门回查原 PDF，而不是回看下游写的值。
    """
    docs = {d.file_name: d for d in documents_of(case, root=root)}
    cache: dict[tuple[str, int], str] = {}
    docs_opened: dict[str, object] = {}

    def read(file_name: str, page: int | None) -> str | None:
        d = docs.get(str(file_name))
        if d is None or not d.content_path or page is None:
            return None
        path = Path(d.content_path)
        if not path.is_file():
            return None
        key = (str(file_name), int(page))
        if key in cache:
            return cache[key]
        try:
            import pymupdf
        except ImportError:                                   # pragma: no cover
            return None
        doc = docs_opened.get(str(file_name))
        if doc is None:
            try:
                doc = pymupdf.open(str(path))
            except Exception:                                 # pragma: no cover
                return None
            docs_opened[str(file_name)] = doc
        n = int(page)
        if not (1 <= n <= doc.page_count):                     # type: ignore[attr-defined]
            cache[key] = ""
            return ""
        text = doc[n - 1].get_text()                            # type: ignore[attr-defined]
        cache[key] = text
        return text

    return read
