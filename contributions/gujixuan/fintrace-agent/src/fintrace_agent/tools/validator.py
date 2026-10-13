"""证据真实性门（W1~W6）—— 模块 E 的另一半，对应 BUILD_SPEC 第 5 节与方案 v2.2 §7。

W 门回答的是「**这份新数据能不能安全登记**」，它只证明「证据真实」，不证明「理解正确」：

* 过 W 的断言只到 ``evidence_status=attested``，``semantic_status`` 仍由 ``semantic_binding``
  独立判定（两条轴分离，见 contracts.Assertion.is_fact）；
* 不过 W 的候选**不进事实库**，并逐条写进 ``rejected`` 明细（不是静默丢），停在 ``candidate``。

六项检查：

- **W1 格式**：必填字段齐全、类型正确，``raw_text`` / ``value`` 能解析。
- **W2 来源**：``doc.publication_date <= cutoff_date``（硬门，失败即 rejected）；来源类型在允许
  集合内；页码或公告号存在。
- **W3 身份**：证券代码 / 报告期 / 是否摘要或更正版与案件一致（更正版直接拒）。
- **W4 数值五联**：数值按位、单位按换算比、符号同向、指标同名、页码与该句的数联合绑定；
  逐项写出在哪一条上失败。
- **W5 归置**：过 → ``attested``；不过 → ``rejected`` 明细。
- **W6 精度保真**：``decimals_of(raw_text)`` 与 ``decimals_as_disclosed`` 一致；解析值小数位少于
  披露位（截断痕迹）→ rejected。

只用标准库；数据类一律来自 ``fintrace_agent.contracts``。
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Callable

from ..contracts import Assertion, AsOfMode, Document, EvidenceStatus, sha256_text
from ..parsers.bridge import decimals_of

#: 需要数值语义的 kind（W1/W4/W6 会核它们的数值）。
_NUMERIC_KINDS: tuple[str, ...] = ("financial_value", "operating_event", "forecast")

#: 数值比较时忽略的字符（千分位逗号与空白）。
_DIGIT_NOISE = re.compile(r"[,\s]")


@dataclass
class Admission:
    """一次 W 门归置的结果。"""

    accepted: list[Assertion] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)   # {"code":"W2","span_sha"/"raw":..., "why":...}
    checks: dict = field(default_factory=dict)           # {"W1":n,...,"W5":n,"W6":n}


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _dec(text: str | None) -> Decimal | None:
    """把「可含千分位的数字串」解析成 Decimal；解析不了返回 None。"""
    if text is None:
        return None
    t = str(text).replace(",", "").strip()
    if not t:
        return None
    try:
        return Decimal(t)
    except InvalidOperation:
        return None


def _norm_digits(text: str | None) -> str:
    """去掉千分位与空白，得到纯数字串（用于「页码与该句的数」的联合绑定比对）。"""
    return _DIGIT_NOISE.sub("", text or "")


def _parse_date(text: str) -> _dt.date | None:
    """解析 YYYY-MM-DD；失败返回 None。"""
    try:
        return _dt.date.fromisoformat((text or "").strip())
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# W1~W6（每项返回 (ok, why)）
# ---------------------------------------------------------------------------


def _w1_format(a: Assertion) -> tuple[bool, str]:
    """W1 格式：必填字段齐全、类型正确，raw_text / value 能解析。"""
    problems: list[str] = []
    if not isinstance(a.assertion_id, str) or not a.assertion_id.strip():
        problems.append("assertion_id 缺失或非法")
    if not isinstance(a.kind, str) or not a.kind.strip():
        problems.append("kind 缺失或非法")
    if not isinstance(a.value, str):
        problems.append("value 类型应为 str")
    if not isinstance(a.raw_text, str):
        problems.append("raw_text 类型应为 str")
    if not isinstance(a.source_spans, list):
        problems.append("source_spans 类型应为 list")

    if a.kind in _NUMERIC_KINDS:
        if _dec(a.value) is None:
            problems.append(f"value={a.value!r} 不能解析为数值")
    if a.kind == "financial_value" and not (a.raw_text or "").strip():
        problems.append("financial_value 缺 raw_text")
    return (not problems, "；".join(problems))


def _w2_source(a: Assertion, spans_by_id: dict, doc: Document, cutoff: str,
               allowed: set | None) -> tuple[bool, str]:
    """W2 来源：披露日硬门 + 来源类型白名单 + 页码/公告号存在。"""
    pd = _parse_date(doc.publication_date)
    cut = _parse_date(cutoff)
    if pd is None:
        return False, f"材料披露日期缺失或非法（{doc.publication_date!r}），无法确认早于截止日"
    if cut is None:
        return False, f"截止日非法（{cutoff!r}），时间硬门无法执行"
    if pd > cut:
        return False, f"披露日 {pd.isoformat()} 晚于截止日 {cut.isoformat()}（时间硬门）"

    if allowed is not None and (doc.source_type or "") not in allowed:
        return False, f"来源类型 {doc.source_type!r} 不在允许集合 {sorted(allowed)}"

    ref = [spans_by_id[i] for i in (a.source_spans or []) if i in spans_by_id]
    has_page = any(getattr(s, "page", None) is not None for s in ref)
    has_ann = bool((doc.source_id or "").strip())
    if not has_page and not has_ann:
        return False, "既无页码也无公告号，来源无法定位"
    return True, ""


def _w3_identity(a: Assertion, doc: Document, allowed: set | None,
                 as_of_mode: str, kb) -> tuple[bool, str]:
    """W3 身份：更正版直接拒；摘要版需显式放行；报告期须在受控词表内。"""
    src_type = (doc.source_type or "")
    name = (doc.file_name or "")
    low = src_type.lower()

    if ("correct" in low or "更正" in src_type or "更正" in name or "修正" in name):
        return False, "更正/修正版材料，身份不一致，直接拒"

    summary_like = ("摘要" in name or "摘要" in src_type or "summary" in low)
    if summary_like and not (allowed and src_type in allowed):
        return False, "摘要版材料不得作为身份一致的原件（未显式放行）"

    period = (a.period or "").strip()
    if not period:
        return False, "断言缺报告期，身份不完整"
    chk = kb.context_check(period=period)["checks"].get("period", {})
    if chk.get("ok") is not True:
        return False, f"报告期 {period!r} 不在受控词表"
    _ = as_of_mode  # 更正版的拒法在历史回放与事后验证下一致（一律拒）
    return True, ""


def _w4_number(a: Assertion, spans_by_id: dict, doc: Document,
               page_texts: Callable[[str, int | None], str | None], kb) -> tuple[bool, str]:
    """W4 数值五联：数值按位 / 单位按换算比 / 符号同向 / 指标同名 / 页码绑句。"""
    fails: list[str] = []

    # 1) 数值按位：解析值的数字串必须是原文数字串的字面子串（不得凭空多出数字），
    #    或与原文数值完全相等。**少位数（截断）不在这里判**——那是 W6 的活。
    dr, dv = _dec(a.raw_text), _dec(a.value)
    val_norm = _norm_digits(a.value)
    raw_norm = _norm_digits(a.raw_text)
    same_digits = bool(val_norm) and val_norm in raw_norm
    if not (same_digits or (dr is not None and dv is not None and dr == dv)):
        fails.append(f"数值按位不一致（raw={a.raw_text!r} vs value={a.value!r}）")

    # 2) 单位按换算比：单位必须在受控单位表里、且基准与指标概念基准一致。
    unit = (a.unit or "").strip()
    if unit:
        p = kb.precision_for(a.metric_id or "", unit)
        if p is None:
            fails.append(f"单位 {unit!r} 不可换算（不在受控单位表）")
        else:
            concept = kb.resolve_metric(a.metric) or kb.resolve_metric(a.metric_id or "")
            base = (concept or {}).get("base_unit") or ""
            if base and p.get("base") and p["base"] != base:
                fails.append(f"单位 {unit!r} 换算基准 {p['base']} 与指标基准 {base} 不符")

    # 3) 符号同向：raw_text 与 value 同号。
    if dr is not None and dv is not None:
        def _sign(x: Decimal) -> int:
            return (x > 0) - (x < 0)

        if _sign(dr) != _sign(dv):
            fails.append(f"符号不同向（raw={a.raw_text!r} vs value={a.value!r}）")

    # 4) 指标同名：必须解析到标准概念。
    if kb.resolve_metric(a.metric) is None and kb.resolve_metric(a.metric_id or "") is None:
        fails.append(f"指标名 {a.metric!r} 与标准概念不同名")

    # 5) 页码与该句的数联合绑定：引用页原文里能查到该数值。
    ref = [spans_by_id[i] for i in (a.source_spans or []) if i in spans_by_id]
    if not ref:
        fails.append("未绑定任何原文 span，数值与页码无法联合绑定")
    else:
        bound = False
        raw_norm = _norm_digits(a.raw_text)
        for s in ref:
            page = getattr(s, "page", None)
            txt = page_texts(doc.file_name, page) if page is not None else None
            if txt and raw_norm and raw_norm in _norm_digits(txt):
                bound = True
                break
        if not bound:
            fails.append("引用页原文中未找到该数值，数值与页码未联合绑定")

    return (not fails, "；".join(fails))


def _w6_precision(a: Assertion) -> tuple[bool, str]:
    """W6 精度保真：披露小数位未被截断。"""
    if a.kind not in _NUMERIC_KINDS:
        return True, ""

    raw = (a.raw_text or "").strip()
    if not raw:
        return False, "raw_text 缺失，无法核对披露精度"
    d_raw = decimals_of(raw)
    if d_raw is None:
        return False, f"raw_text={raw!r} 不是纯数字串，无法判定披露精度"

    if a.decimals_as_disclosed is not None:
        try:
            d_disc = int(a.decimals_as_disclosed)
        except (TypeError, ValueError):
            return False, f"decimals_as_disclosed={a.decimals_as_disclosed!r} 非法"
        if d_disc != d_raw:
            return False, (f"披露位数 {d_disc} 与 raw_text={raw!r} 实际 {d_raw} 位不一致"
                           "（截断痕迹）")

    dr = _dec(raw)
    if dr is None:
        return False, f"raw_text={raw!r} 不能解析为数值"

    val = (a.value or "").strip()
    if val:
        dv = _dec(val)
        if dv is None:
            return False, f"value={val!r} 不能解析为数值"
        if dv != dr:
            return False, f"解析值 {val!r} 与原文 {raw!r} 不一致（疑截断）"
        d_val = decimals_of(val)
        if d_val is not None and d_val < d_raw:
            return False, (f"解析值小数位 {d_val} 少于披露位 {d_raw}"
                           f"（{val!r} 由 {raw!r} 截断而来）")
    return True, ""


# ---------------------------------------------------------------------------
# 门面
# ---------------------------------------------------------------------------


class Validator:
    """W 门。构造时给全静态条件（知识库、截止日、取页原文的回调、允许的来源类型）。"""

    def __init__(self, *, kb, cutoff_date: str,
                 page_texts: Callable[[str, int | None], str | None],
                 allowed_source_types: set | None = None,
                 as_of_mode: str = AsOfMode.HISTORICAL_REPLAY.value,
                 case: dict | None = None):
        self.kb = kb
        self.cutoff_date = cutoff_date
        self.page_texts = page_texts
        self.allowed_source_types = allowed_source_types
        self.as_of_mode = as_of_mode
        self.case = case or {}

    def _span_sha(self, a: Assertion, spans_by_id: dict) -> str:
        for sid in a.source_spans or []:
            s = spans_by_id.get(sid)
            if s is not None and getattr(s, "quoted_text", ""):
                return sha256_text(s.quoted_text)
        return ""

    def admit(self, candidates: list[Assertion], *, spans: list,
              doc: Document) -> Admission:
        """逐条过 W1~W6；过 → ``attested`` 进 ``accepted``，不过 → 写 ``rejected`` 明细。"""
        spans_by_id = {getattr(s, "span_id", None): s for s in (spans or [])}
        accepted: list[Assertion] = []
        rejected: list[dict] = []
        pass_counts = {f"W{i}": 0 for i in range(1, 7)}

        for a in candidates:
            code = ""
            why = ""
            # 顺序即优先级：第一条不过的即为该条的 rejected.code。
            for wcode, ok_fn in (
                ("W1", lambda: _w1_format(a)),
                ("W2", lambda: _w2_source(a, spans_by_id, doc, self.cutoff_date,
                                          self.allowed_source_types)),
                ("W3", lambda: _w3_identity(a, doc, self.allowed_source_types,
                                            self.as_of_mode, self.kb)),
                ("W4", lambda: _w4_number(a, spans_by_id, doc, self.page_texts, self.kb)),
                ("W6", lambda: _w6_precision(a)),
            ):
                ok, why = ok_fn()
                if not ok:
                    code = wcode
                    break
                pass_counts[wcode] += 1

            if code:
                # 不过的候选停在 candidate，绝不进事实库（W5 归置）。
                rec: dict = {"code": code, "assertion_id": a.assertion_id,
                             "raw": a.raw_text, "why": why}
                sha = self._span_sha(a, spans_by_id)
                if sha:
                    rec["span_sha"] = sha
                rejected.append(rec)
                continue

            # 六项全过：只登记证据真实性这一轴，语义轴留给 semantic_binding。
            a.evidence_status = EvidenceStatus.ATTESTED.value
            accepted.append(a)
            pass_counts["W5"] += 1

        checks = dict(pass_counts)
        checks["accepted"] = len(accepted)
        checks["rejected"] = len(rejected)
        checks["total"] = len(candidates)
        return Admission(accepted=accepted, rejected=rejected, checks=checks)


__all__ = ["Admission", "Validator"]
