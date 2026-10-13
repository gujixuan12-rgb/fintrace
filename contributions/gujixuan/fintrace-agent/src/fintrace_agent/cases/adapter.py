"""案件适配（v2.2 §9）：把案件 JSON、草稿、材料翻成契约对象。

只做搬运与形状转换，不做判定。判定在引擎与门里。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from fintrace_agent import paths as P
from fintrace_agent.contracts import (
    Acquisition,
    Claim,
    ClaimStatus,
    Document,
    MaterialFormat,
    sha256_bytes,
)

#: 草稿拆解出的断言中，这些 metric_key 视为预测类（不得进矛盾判定）
FORECAST_HINT = ("forecast", "prediction", "estimate")


@dataclass
class CaseInput:
    case_id: str
    company: str = ""
    stock_code: str = ""
    period: str = ""
    prediction_cutoff_date: str = ""
    cutoff_rationale: str = ""
    draft_path: str = ""
    expected_path: str = ""
    documents: list[dict] = field(default_factory=list)
    raw: dict = field(default_factory=dict)
    case_path: str = ""

    def to_dict(self) -> dict:
        return {"case_id": self.case_id, "company": self.company,
                "stock_code": self.stock_code, "period": self.period,
                "prediction_cutoff_date": self.prediction_cutoff_date,
                "draft_path": self.draft_path, "expected_path": self.expected_path,
                "documents": list(self.documents)}


def _abs(rel: str, root: Path | None = None) -> Path:
    """案件里的路径是相对项目根的，统一在这里解析。"""
    p = Path(rel)
    return p if p.is_absolute() else (root or P.ROOT) / p


def load_case(path: str | Path, *, root: Path | None = None) -> CaseInput:
    p = _abs(str(path), root)
    data = json.loads(p.read_text(encoding="utf-8"))
    return CaseInput(
        case_id=str(data.get("case_id") or p.stem),
        company=str(data.get("company") or ""),
        stock_code=str(data.get("stock_code") or ""),
        period=str(data.get("period") or ""),
        prediction_cutoff_date=str(data.get("prediction_cutoff_date") or ""),
        cutoff_rationale=str(data.get("cutoff_rationale") or ""),
        draft_path=str(data.get("draft") or ""),
        expected_path=str(data.get("expected") or ""),
        documents=list(data.get("documents") or []),
        raw=data,
        case_path=str(p),
    )


def find_case(case_id: str, *, root: Path | None = None) -> CaseInput:
    """按 case_id 或文件名在 data/annotations/cases 下找案件。"""
    base = (root or P.ROOT) / "data" / "annotations" / "cases"
    for p in sorted(base.glob("*.json")):
        if p.stem == case_id:
            return load_case(p, root=root)
        data = json.loads(p.read_text(encoding="utf-8"))
        if str(data.get("case_id")) == case_id:
            return load_case(p, root=root)
    raise FileNotFoundError(f"找不到案件 {case_id}（在 {base}）")


def list_cases(*, root: Path | None = None) -> list[str]:
    base = (root or P.ROOT) / "data" / "annotations" / "cases"
    return [p.stem for p in sorted(base.glob("*.json"))]


def read_draft(case: CaseInput, *, root: Path | None = None) -> str:
    return _abs(case.draft_path, root).read_text(encoding="utf-8")


def read_expected(case: CaseInput, *, root: Path | None = None) -> dict:
    return json.loads(_abs(case.expected_path, root).read_text(encoding="utf-8"))


def documents_of(case: CaseInput, *, root: Path | None = None) -> list[Document]:
    """材料 → Document。算 content_hash（原文留档的指纹），并落绝对路径。"""
    out: list[Document] = []
    for i, d in enumerate(case.documents):
        rel = str(d.get("path") or "")
        p = _abs(rel, root) if rel else None
        digest = ""
        if p is not None and p.is_file():
            digest = sha256_bytes(p.read_bytes())
        suffix = (p.suffix.lower().lstrip(".") if p else "")
        fmt = {"pdf": MaterialFormat.PDF.value, "docx": MaterialFormat.DOCX.value,
               "doc": MaterialFormat.DOC.value, "txt": MaterialFormat.TEXT.value,
               "md": MaterialFormat.TEXT.value}.get(suffix, MaterialFormat.UNKNOWN.value)
        pub = str(d.get("publication_date") or "")
        out.append(Document(
            doc_id=f"D-{i + 1:02d}",
            source_id=str(d.get("source_id") or ""),
            file_name=str(d.get("file_name") or ""),
            source_type=str(d.get("source_type") or ""),
            publication_date=pub,
            page_count=None,
            acquisition=Acquisition.LOCAL.value,
            content_path=str(p) if p else "",
            content_hash=digest,
            material_format=fmt,
            first_published_at=pub,
        ))
    return out


def _clean_claim_text(text: str) -> str:
    """草稿里拆出来的 claim_text 常带 markdown 标题残留，去掉它。"""
    t = (text or "").strip()
    while t.startswith("#") or t.startswith("-") or t.startswith("\n"):
        lines = [ln for ln in t.splitlines() if ln.strip()]
        t = lines[-1] if lines else t
        t = t.lstrip("#- \n")
    return t.strip()


def draft_to_claims(draft_text: str, *, case: CaseInput | None = None,
                    use_llm: bool = False) -> list[Claim]:
    """草稿 → Claim。默认走引擎的规则拆解（离线可复现）；use_llm 时调引擎模型拆解。

    规则拆解的字段名以引擎实际输出为准：assertion / value / unit / metric_key /
    source_sentence / source_sentence_id / claim_group_id / period_kind。
    """
    from fintrace_engine import draft_review as dr

    if use_llm:
        try:
            from fintrace_engine import llm as engine_llm

            rows = engine_llm.decompose_claims(draft_text)
        except Exception:
            rows = dr.extract_claims_rules(draft_text)
    else:
        rows = dr.extract_claims_rules(draft_text)

    claims: list[Claim] = []
    for i, r in enumerate(rows):
        sentence = str(r.get("source_sentence") or "").strip()
        text = _clean_claim_text(sentence or str(r.get("claim_text") or r.get("assertion") or ""))
        gid = str(r.get("claim_group_id") or r.get("source_sentence_id") or f"{i + 1:03d}")
        metric_key = str(r.get("metric_key") or "")
        kind_forecast = bool(r.get("is_forecast")) or any(h in metric_key for h in FORECAST_HINT)
        claims.append(Claim(
            claim_id=f"C{gid}",
            claim_text=text,
            source_sentence=sentence or text,
            sentence_id=str(r.get("source_sentence_id") or ""),
            subject=(case.company if case else ""),
            period=str(r.get("period_kind") or (case.period if case else "")),
            metric_key=metric_key,
            claim_value=(None if r.get("value") in (None, "") else str(r.get("value"))),
            claim_unit=str(r.get("unit") or ""),
            status=ClaimStatus.OPEN.value,
            open_questions=(["预测类主张：只核基期，不做事实判定"] if kind_forecast else []),
        ))
    return claims
