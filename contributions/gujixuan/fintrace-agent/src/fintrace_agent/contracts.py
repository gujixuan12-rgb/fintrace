"""FinTrace 独立版 · 统一数据契约（对应方案 v2.2 §2、§3）。

规矩：这是全项目唯一的数据定义处。任何模块不得自造字段、不得改本文件（要加字段先报给主代理）。
只用标准库；核心链路零第三方依赖是这个项目的不变量。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional

SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# 枚举（v2.2 §13 A）
# ---------------------------------------------------------------------------


class SpanKind(str, Enum):
    TABLE_CELL = "table_cell"
    TABLE_ROW = "table_row"
    PARAGRAPH = "paragraph"
    TITLE = "title"
    NOTE = "note"


class MaterialFormat(str, Enum):
    PDF = "pdf"
    DOCX = "docx"
    DOC = "doc"
    TEXT = "text"
    UNKNOWN = "unknown"


class Acquisition(str, Enum):
    LOCAL = "local"
    CNINFO = "cninfo"
    WEB = "web"


class ExtractorTier(str, Enum):
    BUILTIN = "builtin"        # 已审的内置提取器
    DOCX = "docx"              # 零依赖 docx 提取器
    SPEC = "spec"              # 声明式抽取规格 t_spec_extract
    AUTHORED = "authored"      # 自写代码（未启用，见 5.4）


class Origin(str, Enum):
    """断言来源三分法：只有 material 能当出处。"""

    MATERIAL = "material"
    DRAFT = "draft"
    ENGINE = "engine"


class EvidenceStatus(str, Enum):
    """轴一：证据真实性（W 门）。"""

    CANDIDATE = "candidate"
    ATTESTED = "attested"
    REJECTED = "rejected"


class SemanticStatus(str, Enum):
    """轴二：语义绑定正确性（独立检查，B1~B4）。"""

    UNBOUND = "unbound"
    BOUND = "bound"
    CONFLICTED = "conflicted"


class AssertionKind(str, Enum):
    FINANCIAL_VALUE = "financial_value"
    OPERATING_EVENT = "operating_event"
    FACT_STATEMENT = "fact_statement"
    FORECAST = "forecast"
    OPINION = "opinion"


class RelationPredicate(str, Enum):
    CONTRADICTS = "contradicts"
    EQUALS = "equals"
    DERIVES_FROM = "derives_from"
    SUPPORTED_BY = "supported_by"
    CHANGES_BY = "changes_by"
    BELONGS_TO = "belongs_to"


class MatchResult(str, Enum):
    """适用条件逐字段的匹配结论 —— 只能由程序填（v2.2 §2.4）。"""

    VERIFIED = "verified"
    FAILED = "failed"
    UNKNOWN = "unknown"


class Verdict(str, Enum):
    """判定四类（内部口径，engine 沿用）。"""

    SUPPORTED = "SUPPORTED"
    CONTRADICTED = "CONTRADICTED"
    INSUFFICIENT = "INSUFFICIENT"
    PREDICTION = "PREDICTION"


class ReportLabel(str, Enum):
    """对外报告标签五类（schemas/claim_record.schema.json）。"""

    RISK_SIGNAL_DETECTED = "RISK_SIGNAL_DETECTED"
    FURTHER_VERIFICATION_SUGGESTED = "FURTHER_VERIFICATION_SUGGESTED"
    POSSIBLE_RISK_CATEGORY = "POSSIBLE_RISK_CATEGORY"
    INSUFFICIENT_INFORMATION = "INSUFFICIENT_INFORMATION"
    CONFIRMED_BY_AUTHORITATIVE_DOCUMENT = "CONFIRMED_BY_AUTHORITATIVE_DOCUMENT"


class Publish(str, Enum):
    ALLOWED = "allowed"
    BLOCKED = "blocked"
    MANUAL_REVIEW = "manual_review"


class BoundDecision(str, Enum):
    ALLOW = "allow"
    WARN = "warn"
    BLOCK = "block"
    HALT = "halt"


class GuardCode(str, Enum):
    """打转守门五类（移植自 Hermes agent/tool_guardrails.py，MIT）。"""

    EXACT_FAILURE = "exact_failure"
    SAME_TOOL_FAILURE = "same_tool_failure"
    IDEMPOTENT_NO_PROGRESS = "idempotent_no_progress"
    IDENTICAL_CALL_STREAK = "identical_call_streak"
    IDENTICAL_CYCLE = "identical_cycle"


class ClaimStatus(str, Enum):
    OPEN = "open"
    EVIDENCED = "evidenced"
    VERIFIED = "verified"
    INSUFFICIENT = "insufficient"
    BLOCKED = "blocked"


class LeadStatus(str, Enum):
    LEAD = "lead"
    OFFICIAL_REF = "official_ref"
    SECONDARY = "secondary"
    DISCARDED = "discarded"


class PlannerMode(str, Enum):
    AGENT = "agent"
    FIXED = "fixed"


class AsOfMode(str, Enum):
    HISTORICAL_REPLAY = "historical_replay"
    POST_HOC_REVIEW = "post_hoc_review"


class ProgressKind(str, Enum):
    """「进展」四类 + 无进展（v2.2 §9.2）。被否决的证据也算进展。"""

    CLAIM_STATUS_CHANGE = "claim_status_change"
    HYPOTHESIS_ELIMINATED = "hypothesis_eliminated"
    NEW_EVIDENCE = "new_evidence"
    EVIDENCE_REJECTED = "evidence_rejected"
    NONE = "none"


#: 判定（内部四类）→ 报告标签（对外五类），v2.2 §2.6 的唯一映射处
LABEL_MAP: dict[Verdict, Optional[ReportLabel]] = {
    Verdict.CONTRADICTED: ReportLabel.RISK_SIGNAL_DETECTED,
    Verdict.SUPPORTED: None,
    Verdict.INSUFFICIENT: ReportLabel.INSUFFICIENT_INFORMATION,
    Verdict.PREDICTION: None,
}

#: 动作白名单（v2.2 §13 A）
ACTIONS: tuple[str, ...] = (
    "t_search_official", "t_search_web", "t_fetch_web", "t_parse", "t_extract",
    "t_knowledge", "t_compute", "t_review", "t_lookup", "t_recall",
    "t_spec_extract", "t_author_tool", "stop",
)
#: 默认关闭的动作（未隔离前不启用 / 未配置）
ACTIONS_DISABLED_BY_DEFAULT: tuple[str, ...] = ("t_author_tool",)


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(obj: Any) -> str:
    """稳定序列化：可复现性依赖它（同一 state 必须给出同一 digest）。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(obj: Any) -> str:
    return sha256_text(canonical_json(obj))[:16]


def _drop_none(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


# ---------------------------------------------------------------------------
# 材料层
# ---------------------------------------------------------------------------


@dataclass
class Document:
    """v2.2 §2.1 —— 一份材料。原文必须留档，不删除、不覆盖。"""

    doc_id: str
    source_id: str
    file_name: str
    source_type: str
    publication_date: str = ""          # YYYY-MM-DD；空串会被时间硬门判为不可用
    page_count: Optional[int] = None
    acquisition: str = Acquisition.LOCAL.value
    content_path: str = ""
    content_hash: str = ""
    available_before_cutoff: Optional[bool] = None
    material_format: str = MaterialFormat.UNKNOWN.value
    first_published_at: str = ""        # 首次公开时间（线索用；材料缺省等于 publication_date）
    retrieved_at: str = ""              # 抓取时间（本地文件为空）
    snapshot_hash: str = ""
    version: str = ""
    superseded_by: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Document":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})  # type: ignore[arg-type]


@dataclass
class Span:
    """v2.2 §2.2 —— 可精确定位的原文片段。只追加。"""

    span_id: str
    doc_id: str
    page: Optional[int]
    char_range: tuple[int, int] = (0, 0)
    quoted_text: str = ""
    decimals_as_disclosed: Optional[int] = None   # 披露几位小数就存几位，禁止截断
    span_kind: str = SpanKind.PARAGRAPH.value
    bound_context: dict = field(default_factory=dict)   # row_label/col_label/unit/period
    rect: Optional[tuple[float, float, float, float]] = None
    extractor: str = ExtractorTier.BUILTIN.value
    spec_id: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["char_range"] = list(self.char_range)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Span":
        d = dict(d)
        if d.get("char_range"):
            d["char_range"] = tuple(d["char_range"])
        if d.get("rect"):
            d["rect"] = tuple(d["rect"])
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 断言层 / 关系层
# ---------------------------------------------------------------------------


@dataclass
class Assertion:
    """v2.2 §2.3 —— 两轴状态：证据真实性 + 语义绑定。"""

    assertion_id: str
    kind: str
    subject: str = ""
    period: str = ""
    metric: str = ""
    metric_id: str = ""
    value: str = ""                     # 字符串保存，解析成 Decimal 时用全精度
    unit: str = ""
    caliber: str = ""
    raw_text: str = ""                  # 原文原样
    decimals_as_disclosed: Optional[int] = None
    direction: Optional[str] = None
    text: str = ""
    source_spans: list[str] = field(default_factory=list)
    extracted_by: dict = field(default_factory=dict)     # how/model/prompt_version
    origin: str = Origin.MATERIAL.value
    evidence_status: str = EvidenceStatus.CANDIDATE.value
    semantic_status: str = SemanticStatus.UNBOUND.value
    gate_trace: dict = field(default_factory=dict)       # w_checks/binding_checks
    claim_id: str = ""
    run_id: str = ""

    @property
    def is_fact(self) -> bool:
        """两轴都过才算事实（v2.2 §2.3）。"""
        return (self.evidence_status == EvidenceStatus.ATTESTED.value
                and self.semantic_status == SemanticStatus.BOUND.value)

    @property
    def can_support_contradiction(self) -> bool:
        return self.is_fact and self.origin == Origin.MATERIAL.value

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Assertion":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})  # type: ignore[arg-type]


@dataclass
class ApplicabilityField:
    """适用条件的单个字段：(主张值, 证据值/出处, 匹配结论)。match 只能由程序填。"""

    claim: Any = None
    evidence: Any = None
    match: str = MatchResult.UNKNOWN.value
    note: str = ""

    def to_dict(self) -> dict:
        return _drop_none(asdict(self))


@dataclass
class Relation:
    """v2.2 §2.4 —— 关系必须满足适用条件，方案维度不可省。"""

    relation_id: str
    predicate: str
    subject_ref: str
    object_ref: str
    applicability: dict = field(default_factory=dict)    # 字段名 -> ApplicabilityField|dict
    rule_id: str = ""
    rule_version: str = ""
    status: str = "candidate"
    verified_by: dict = field(default_factory=dict)
    run_id: str = ""

    def field_ok(self, name: str) -> bool:
        f = self.applicability.get(name)
        if isinstance(f, ApplicabilityField):
            return f.match == MatchResult.VERIFIED.value
        if isinstance(f, dict):
            return f.get("match") == MatchResult.VERIFIED.value
        return False

    @property
    def applicable(self) -> bool:
        """任一字段 failed 即不适用；unknown 视为未验证（不得判矛盾）。"""
        if not self.applicability:
            return False
        vals = [f if isinstance(f, dict) else f.to_dict() for f in self.applicability.values()]
        return all(v.get("match") == MatchResult.VERIFIED.value for v in vals)

    @property
    def failed_fields(self) -> list[str]:
        out = []
        for name, f in self.applicability.items():
            v = f if isinstance(f, dict) else f.to_dict()
            if v.get("match") == MatchResult.FAILED.value:
                out.append(name)
        return out

    def to_dict(self) -> dict:
        d = asdict(self)
        d["applicability"] = {
            k: (v.to_dict() if isinstance(v, ApplicabilityField) else v)
            for k, v in self.applicability.items()
        }
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Relation":
        d = dict(d)
        d["applicability"] = {
            k: (ApplicabilityField(**v) if isinstance(v, dict) else v)
            for k, v in (d.get("applicability") or {}).items()
        }
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 待核主张 / 线索 / 收据 / 决策
# ---------------------------------------------------------------------------


@dataclass
class Claim:
    """待核对象（来自研报草稿），不是 Assertion。"""

    claim_id: str
    claim_text: str
    source_sentence: str = ""
    sentence_id: str = ""
    subject: str = ""
    period: str = ""
    metric_key: str = ""
    claim_value: Optional[str] = None
    claim_unit: str = ""
    status: str = ClaimStatus.OPEN.value
    open_questions: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    finding_id: str = ""
    blocked_reason: Optional[str] = None

    @property
    def resolved(self) -> bool:
        return self.status in (ClaimStatus.VERIFIED.value, ClaimStatus.INSUFFICIENT.value,
                               ClaimStatus.BLOCKED.value)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Claim":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})  # type: ignore[arg-type]


@dataclass
class Lead:
    """v2.2 §5.2 —— 联网线索。线索不是证据，且受 as_of 过滤。"""

    lead_id: str
    url: str = ""
    title: str = ""
    snippet: str = ""
    source_domain: str = ""
    first_published_at: str = ""       # 首次公开时间，不是抓取时间
    retrieved_at: str = ""
    version: str = ""
    snapshot_hash: str = ""
    superseded_by: str = ""
    official_ref: Optional[str] = None  # 指向官方原件的公告号 / PDF URL
    status: str = LeadStatus.LEAD.value

    @property
    def usable_as_evidence(self) -> bool:
        return self.status == LeadStatus.OFFICIAL_REF.value and bool(self.official_ref)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ToolReceipt:
    """工具返回的标准收据 —— W 门的输入。"""

    tool: str
    ok: bool
    result: dict = field(default_factory=dict)
    error: str = ""
    ms: int = 0
    blob_refs: list[str] = field(default_factory=list)    # 大对象走内容寻址
    new_evidence: list[str] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    progress: str = ProgressKind.NONE.value

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Decision:
    """v2.2 §9.2 —— 决策日志要能重放，所以存完整内容而不是摘要。"""

    run_id: str
    n: int
    action: str
    args: dict = field(default_factory=dict)
    why: str = ""
    raw_response: str = ""
    prompt_version: str = ""
    model: str = ""
    state_digest: str = ""
    valid: bool = True
    invalid_reason: str = ""
    bound_decision: str = BoundDecision.ALLOW.value
    as_of: str = ""
    context_leads_span: tuple[str, str] = ("", "")   # 本轮上下文里线索首曝时间区间
    at: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["context_leads_span"] = list(self.context_leads_span)
        return d


@dataclass
class Bounds:
    """硬边界（v2.2 §4.1）。只有否决权，没有说话权。"""

    cutoff_date: str = ""
    max_tool_calls: int = 24
    token_budget: int = 40000
    deadline_ts: float = 0.0
    cancel_requested: bool = False
    allowed_tools: list[str] = field(default_factory=lambda: [
        a for a in ACTIONS if a not in ACTIONS_DISABLED_BY_DEFAULT])
    as_of_mode: str = AsOfMode.HISTORICAL_REPLAY.value

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# 结论层
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    """v2.2 §3.4 —— V 门的检查对象，也是报告的最小单位。"""

    finding_id: str
    claim_id: str
    verdict: str
    error_type: str = ""
    basis: str = ""
    suggested_fix: str = ""
    evidence: list[dict] = field(default_factory=list)
    assertion_refs: list[str] = field(default_factory=list)   # 显式引用：V5 查的就是它
    relation_refs: list[str] = field(default_factory=list)    # 显式引用：V3 查的就是它
    citation_pages: list[int] = field(default_factory=list)
    correction: dict = field(default_factory=dict)
    audit: dict = field(default_factory=dict)          # w_passed / v_checks
    publish: str = Publish.MANUAL_REVIEW.value
    block_reasons: list[str] = field(default_factory=list)
    report_labels: list[str] = field(default_factory=list)
    rules_version: str = ""
    rule_id: str = ""
    rounding_step: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Finding":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})  # type: ignore[arg-type]


@dataclass
class Residual:
    """未解决清单的一条（V6：报告必须带它）。"""

    claim_id: str = ""
    kind: str = ""                 # unresolved_claim / open_question / failed_tool / blocked
    text: str = ""
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# 任务状态
# ---------------------------------------------------------------------------


@dataclass
class TaskState:
    """v2.2 §3.1 —— 只存目标/进度/待解问题/证据索引，不存正文。"""

    run_id: str
    schema_version: int = SCHEMA_VERSION
    artifact_fingerprint: str = ""
    mode: str = PlannerMode.AGENT.value
    goal: dict = field(default_factory=dict)           # task/case_id/company/period
    bounds: Bounds = field(default_factory=Bounds)
    counting: dict = field(default_factory=lambda: {
        "tool_calls": 0, "tokens": 0, "sig_repeats": 0, "invalid_decisions": 0})
    claims: list[Claim] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)
    hypotheses: list[dict] = field(default_factory=list)
    evidence_index: list[str] = field(default_factory=list)   # assertion_id / span_id
    rejected_index: list[str] = field(default_factory=list)
    stop: dict = field(default_factory=lambda: {"requested_by_model": False, "reason": None})
    as_of: str = ""
    unresolved: list[Residual] = field(default_factory=list)
    tool_results: list[str] = field(default_factory=list)     # blob refs

    @property
    def open_claims(self) -> list[Claim]:
        return [c for c in self.claims if not c.resolved]

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "schema_version": self.schema_version,
            "artifact_fingerprint": self.artifact_fingerprint,
            "mode": self.mode,
            "goal": self.goal,
            "bounds": self.bounds.to_dict(),
            "counting": dict(self.counting),
            "claims": [c.to_dict() for c in self.claims],
            "steps": list(self.steps),
            "hypotheses": list(self.hypotheses),
            "evidence_index": list(self.evidence_index),
            "rejected_index": list(self.rejected_index),
            "stop": dict(self.stop),
            "as_of": self.as_of,
            "unresolved": [r.to_dict() for r in self.unresolved],
            "tool_results": list(self.tool_results),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TaskState":
        return cls(
            run_id=d["run_id"],
            schema_version=int(d.get("schema_version", SCHEMA_VERSION)),
            artifact_fingerprint=d.get("artifact_fingerprint", ""),
            mode=d.get("mode", PlannerMode.AGENT.value),
            goal=dict(d.get("goal") or {}),
            bounds=Bounds(**(d.get("bounds") or {})),
            counting=dict(d.get("counting") or {}),
            claims=[Claim.from_dict(c) for c in (d.get("claims") or [])],
            steps=list(d.get("steps") or []),
            hypotheses=list(d.get("hypotheses") or []),
            evidence_index=list(d.get("evidence_index") or []),
            rejected_index=list(d.get("rejected_index") or []),
            stop=dict(d.get("stop") or {"requested_by_model": False, "reason": None}),
            as_of=d.get("as_of", ""),
            unresolved=[Residual(**r) for r in (d.get("unresolved") or [])],
            tool_results=list(d.get("tool_results") or []),
        )


__all__ = [n for n in dir() if not n.startswith("_")]
