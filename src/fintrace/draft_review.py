"""研报草稿核查闭环（当前主线方向）。

方向依据（队长 2026-10-02 17:03 群内拍板原文）：
    「主输入是给定研报草稿，系统核查其中的主张。」

链路：

    草稿正文
      → ① 主张拆解（大模型；模型不可用时退化为确定性抽取）
      → ② 材料登记 + 截止日过滤（代码级硬门）
      → ③ 证据检索与页码定位（空白归一化后做数值/关键词检索）
      → ④ 确定性判定（数值比对、单位归一、跨页勾稽、引用页码、口径）
      → ⑤ 标签 + 修改建议 + 置信度
      → ⑥ 措辞守门人（不通过就拒绝出报告）
      → ⑦ 大模型讲人话（可选，不改判定）

判定纪律：模型不参与任何判定。②④⑥ 全部是代码，模型只负责 ① 和 ⑦。

设计取舍：本模块刻意不复用 `EvidenceIndex`，因为真实年报 PDF 的数字会被
换行截断（如 `1,243,188,17` + `0.21`），而 `EvidenceIndex` 是在原始文本上做
数字正则的。这里改为先把整页文本去掉所有空白，再在其上检索，实测能稳稳
定位到被截断的数字。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import llm
from .models import SourceDocument
from .parsers import document_recognizer as dr
from .verification import cutoff_filter as cf
from .verification import wording_guard as wg

ROOT = Path(__file__).resolve().parents[2]

SUPPORTED = "SUPPORTED"
CONTRADICTED = "CONTRADICTED"
INSUFFICIENT = "INSUFFICIENT"

#: 指标登记表。aliases 用于把模型或草稿里的说法归一到同一个指标。
METRICS: dict[str, dict] = {
    "revenue": {"cn": "营业收入", "aliases": ["营业收入"], "unit": "元"},
    "net_profit_parent": {"cn": "归属于上市公司股东的净利润",
                          "aliases": ["归属于上市公司股东的净利润", "归母净利润"], "unit": "元"},
    "basic_eps": {"cn": "基本每股收益", "aliases": ["基本每股收益"], "unit": "元/股"},
    "net_assets_parent": {"cn": "归属于上市公司股东的净资产",
                          "aliases": ["归属于上市公司股东的净资产"], "unit": "元"},
    "total_assets": {"cn": "总资产", "aliases": ["总资产"], "unit": "元"},
    "gross_margin": {"cn": "毛利率", "aliases": ["毛利率"], "unit": "%"},
    "weighted_roe": {"cn": "加权平均净资产收益率",
                     "aliases": ["加权平均净资产收益率", "净资产收益率"], "unit": "%"},
    "selling_expense": {"cn": "销售费用", "aliases": ["销售费用"], "unit": "元"},
    "rd_expense": {"cn": "研发费用", "aliases": ["研发费用"], "unit": "元"},
    "operating_cash_flow": {"cn": "经营活动产生的现金流量净额",
                            "aliases": ["经营活动产生的现金流量净额", "经营活动现金流量净额"], "unit": "元"},
    "cash_dividend": {"cn": "现金分红金额", "aliases": ["现金分红金额", "现金分红总额"],
                      "unit": "元", "rule": "dividend"},
    "total_shares": {"cn": "总股本", "aliases": ["分配预案的股本基数", "总股本", "股份总数"], "unit": "股"},
    "dividend_per_ten": {"cn": "每10股派息数", "aliases": ["每10股派息数"], "unit": "元/10股"},
    "distributable_profit": {"cn": "可分配利润", "aliases": ["可分配利润"], "unit": "元"},
    "pe_ratio": {"cn": "市盈率", "aliases": ["市盈率"], "unit": "倍", "rule": "needs_external"},
}

#: 真值标注里用过的等价指标名，评测时归一到引擎的指标键。
METRIC_KEY_ALIASES = {
    "net_profit_attributable_to_parent": "net_profit_parent",
    "net_assets_attributable_to_parent": "net_assets_parent",
    "operating_cash_flow_net": "operating_cash_flow",
    "weighted_avg_roe": "weighted_roe",
    "cash_dividend_amount": "cash_dividend",
    "basic_eps_with_citation": "basic_eps",
}

#: 单位 → 以该指标基准单位计的倍数。「元」类指标的基准单位是元。
MONEY_SCALE = {"元": 1.0, "千元": 1e3, "万元": 1e4, "亿元": 1e8}
COUNT_SCALE = {"股": 1.0, "万股": 1e4, "亿股": 1e8}
PASSTHROUGH_UNITS = {"%", "个百分点", "倍", "元/股", "元/10股"}

_MONEY_UNITS = tuple(MONEY_SCALE)

#: 数值后面紧跟单位的识别
_UNIT_AFTER = re.compile(r"^(个百分点|元/股|元/10股|万元|亿元|千元|万股|亿股|元|%|股|倍)")
#: 数字识别。必须先试逗号分组：PDF 表格里的相邻列会粘成
#: `1,243,188,170.211,541,122,075.60`，贪婪匹配会把两列吃成一个数。
_NUMBER = re.compile(r"-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|-?\d+\.\d+|-?\d+")
_NUM_STRICT = re.compile(r"^-?\d[\d,]*(?:\.\d+)?$")


def flat(s: str) -> str:
    """去掉全部空白。被换行截断的数字只有在这一步之后才能被检索到。"""
    return re.sub(r"\s+", "", s)


def _to_float(tok: str) -> float | None:
    try:
        return float(tok.replace(",", ""))
    except ValueError:
        return None


def to_base(value: float, unit: str, base_unit: str) -> float | None:
    """把「数值+单位」换算到指标基准单位。单位不认识就返回 None，不许瞎猜。"""
    unit = (unit or "").strip()
    if not unit:
        return value
    if base_unit == "元" and unit in MONEY_SCALE:
        return value * MONEY_SCALE[unit]
    if base_unit == "股" and unit in COUNT_SCALE:
        return value * COUNT_SCALE[unit]
    if unit == base_unit:
        return value
    if base_unit == "元/股" and unit in ("元", "元/股"):
        # 草稿常写「每股收益 0.72 元」而披露口径是「元/股」，同一维度，不算单位错
        return value
    if base_unit == "元/股" and unit in MONEY_SCALE:
        return None
    if unit in PASSTHROUGH_UNITS and base_unit in PASSTHROUGH_UNITS:
        return value
    if base_unit == "元/10股" and unit in ("元", "元/10股"):
        return value
    return None


def _is_year_token(tok: str, value: float) -> bool:
    return "." not in tok and 1900 <= value <= 2100


#: 各基准单位在年报里的小数位数。去空白后相邻列会粘成一个数
#: （`1,243,188,170.211,541,122,075.60`），按精度截断才能还原。
PRECISION = {"元": 2, "股": 0, "元/股": 4, "%": 2, "倍": 2, "元/10股": 2}


def _truncate_decimals(tok: str, digits: int) -> str:
    neg = tok.startswith("-")
    body = tok.lstrip("-")
    if "." not in body:
        return tok
    intpart, dec = body.split(".", 1)
    if len(dec) <= digits:
        return tok
    sign = "-" if neg else ""
    return sign + intpart if digits == 0 else sign + intpart + "." + dec[:digits]


def snippet(page: "PageRec", alias: str, value_raw: str | None = None,
            span: int = 3, maxlen: int = 170) -> str:
    """从原始文本里取可读引用。

    优先取「含有该数值的那一行」及其上下各两行——表格里数值所在行才是证据，
    指标名往往只出现在表头，光按指标名取会取到表头。
    """
    lines = page.raw.splitlines()
    if value_raw:
        needle = flat(value_raw)
        # 数值可能被竖切成两行（`14.1` / `7%`），单行找不到时把相邻行拼起来再找
        for width in (1, 3):
            for i in range(len(lines)):
                if needle and needle in flat("".join(lines[i:i + width])):
                    seg = " / ".join(s.strip()
                                     for s in lines[max(0, i - 1):i + width + 2] if s.strip())
                    return seg[:maxlen]
    needle = flat(alias)
    for i, line in enumerate(lines):
        if needle in flat(line):
            seg = " / ".join(s.strip() for s in lines[i:i + span + 1] if s.strip())
            return seg[:maxlen]
    return ""


# ---------------------------------------------------------------------------
# 材料索引
# ---------------------------------------------------------------------------


@dataclass
class PageRec:
    source_id: str
    file_name: str
    source_type: str
    publication_date: str
    page: int
    raw: str

    @property
    def flat(self) -> str:
        return flat(self.raw)


@dataclass
class Token:
    """页面上的一个数值候选，带它左右两侧的上下文。"""

    value: float
    unit: str
    base: float | None
    raw: str
    page: int
    before: str
    after: str


class MaterialIndex:
    def __init__(self, pages: list[PageRec]) -> None:
        self.pages = pages

    @property
    def flats(self) -> dict[int, str]:
        return {p.page: p.flat for p in self.pages}

    def alias_pages(self, aliases: Iterable[str]) -> list[PageRec]:
        flats = self.flats
        out = []
        for p in self.pages:
            f = flats[p.page]
            if any(flat(a) in f for a in aliases):
                out.append(p)
        return out


def load_pdf_pages(path: str | Path, source: SourceDocument) -> list[PageRec]:
    try:
        import pymupdf
    except ImportError:  # pragma: no cover
        try:
            import fitz as pymupdf  # type: ignore
        except ImportError as exc:
            raise ImportError("读 PDF 需要 pymupdf：pip install pymupdf") from exc
    doc = pymupdf.open(str(path))
    out = [
        PageRec(
            source_id=source.source_id,
            file_name=source.file_name,
            source_type=source.source_type,
            publication_date=source.publication_date,
            page=i + 1,
            raw=doc[i].get_text(),
        )
        for i in range(doc.page_count)
    ]
    doc.close()
    return out


def _number_re(prec: int) -> re.Pattern:
    """按指标精度构造数字正则。

    必须先试逗号分组，且小数位要限长：PDF 表格里相邻列会粘成
    `1,243,188,170.211,541,122,075.60`、`1,064,066,560.8814.17%`，
    不限长就会把 `14.17%` 的 `14` 吃进前一个数里。
    """
    if prec <= 0:
        return re.compile(r"-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|-?\d+\.\d+|-?\d+")
    return re.compile(
        rf"-?\d{{1,3}}(?:,\d{{3}})+(?:\.\d{{1,{prec}}})?|-?\d+\.\d{{1,{prec}}}|-?\d+"
    )


def tokens_near(page: PageRec, aliases: Iterable[str], window: int = 140,
                base_unit: str = "元") -> list[Token]:
    """指标关键词之后 window 个字符内的数值候选。

    这是「疑似该指标的披露值」——不保证配对正确，只作为比对与取证的候选集。
    """
    text = page.flat
    prec = PRECISION.get(base_unit, 2)
    rx = _number_re(prec)
    out: list[Token] = []
    seen: set[tuple[float, int]] = set()
    for alias in aliases:
        needle = flat(alias)
        start = 0
        while True:
            pos = text.find(needle, start)
            if pos < 0:
                break
            start = pos + len(needle)
            chunk = text[start:start + window]
            for m in rx.finditer(chunk):
                raw = _truncate_decimals(m.group(0), prec)
                val = _to_float(raw)
                if val is None or _is_year_token(raw, val):
                    continue
                rest = chunk[m.end():]
                um = _UNIT_AFTER.match(rest)
                unit = um.group(1) if um else ""
                key = (round(val, 6), unit)
                if key in seen:
                    continue
                seen.add(key)
                out.append(Token(value=val, unit=unit, base=None, raw=raw, page=page.page,
                                 before=chunk[:m.start()][-60:], after=rest[:40]))
    return out


# ---------------------------------------------------------------------------
# 主张
# ---------------------------------------------------------------------------


@dataclass
class Claim:
    claim_id: str
    claim_text: str
    metric_key: str | None
    value: float | None
    unit: str
    period: str = ""
    cited_page: int | None = None
    source: str = "llm"
    raw: dict[str, Any] = field(default_factory=dict)


def resolve_metric(text: str, anchor: int | None = None) -> str | None:
    """把任意说法归一到指标键。

    anchor 给出「数值」在 text 中的位置时，取**离数值最近**的指标名，而不是最长的那个。
    否则「公司对应 2023 年归母净利润的市盈率约为 41 倍」会被判成归母净利润。
    """
    t = flat(text)
    if anchor is None:
        anchor_flat = len(t)
    else:
        anchor_flat = len(flat(text[:anchor]))
    best: tuple[float, int, str] | None = None
    for key, spec in METRICS.items():
        for a in spec["aliases"]:
            na = flat(a)
            start = 0
            while True:
                pos = t.find(na, start)
                if pos < 0:
                    break
                start = pos + 1
                dist = anchor_flat - (pos + len(na))
                if dist < 0:  # 指标名在数值之后，视为配不上
                    dist = 800.0 - dist
                cand = (float(dist), -len(na), key)
                if best is None or cand < best:
                    best = cand
    return best[2] if best else None


_SENT_SPLIT = re.compile(r"[。；\n]")


def _sentence_of(text: str, pos: int) -> str:
    lo = 0
    for m in _SENT_SPLIT.finditer(text):
        if m.end() <= pos:
            lo = m.end()
        elif m.start() >= pos:
            break
    hi = len(text)
    for m in _SENT_SPLIT.finditer(text, pos):
        hi = m.start()
        break
    return text[lo:hi].strip()


_FALLBACK = re.compile(r"(-?\d[\d,]*(?:\.\d+)?)\s*(亿元|万元|万股|亿股|元/股|元|%|倍)")


def extract_claims_rules(draft_text: str) -> list[dict]:
    """不给大模型也能跑：按「数值+单位」定位，再向左找指标名。

    拆得比模型粗（同一句里多个指标可能配错），但保证确定性链路可离线复现。
    """
    out: list[dict] = []
    emitted: set[tuple[int, str]] = set()
    for m in _FALLBACK.finditer(draft_text):
        ctx = draft_text[max(0, m.start() - 40):m.start()]
        key = resolve_metric(ctx, len(ctx))
        if key is None:
            continue
        sent_start = draft_text.rfind("。", 0, m.start()) + 1
        if (sent_start, key) in emitted:
            # 同一句里同一指标只取第一个数值（即水平值），
            # 后面的「同比增减%」是同一断言的组成部分，不重复计条。
            continue
        emitted.add((sent_start, key))
        sent = _sentence_of(draft_text, m.start())
        page = None
        pm = None
        for pm in re.finditer(r"第\s*(\d+)\s*页", sent):
            pass
        if pm:
            page = int(pm.group(1))
        out.append({
            "claim_text": sent,
            "metric": METRICS[key]["cn"],
            "value": m.group(1),
            "unit": m.group(2),
            "page": page,
            "source": "rules",
        })
    _dedupe(out)
    return out


def _dedupe(claims: list[dict]) -> None:
    seen: set[tuple[str, str, str]] = set()
    keep = []
    for c in claims:
        k = (str(c.get("metric", "")), str(c.get("value", "")), str(c.get("unit", "")))
        if k in seen:
            continue
        seen.add(k)
        keep.append(c)
    claims[:] = keep


def split_claims(draft_text: str, *, cfg: dict | None = None, use_llm: bool = True,
                 source: str = "") -> tuple[list[dict], str, str | None]:
    """返回 (主张列表, 拆解方式, 降级原因)。"""
    if use_llm:
        try:
            got = llm.decompose_claims(draft_text, source=source, cfg=cfg)
            if got:
                _dedupe(got)
                for c in got:
                    c.setdefault("source", "llm")
                return got, "llm", None
            return [], "llm", "模型没有拆出任何可核验断言"
        except llm.LLMError as exc:
            fallback = extract_claims_rules(draft_text)
            return fallback, "rules_fallback", f"大模型不可用，已退化为规则抽取：{exc}"
    return extract_claims_rules(draft_text), "rules", None


def _to_claim(d: dict, i: int) -> Claim:
    key = resolve_metric(str(d.get("metric", ""))) or resolve_metric(str(d.get("claim_text", "")))
    raw_val = d.get("value")
    value = None
    if raw_val is not None:
        value = _to_float(str(raw_val))
    page = d.get("page")
    try:
        page = int(page) if page not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        page = None
    return Claim(
        claim_id=f"DRAFT-C{i:03d}",
        claim_text=str(d.get("claim_text", "")).strip(),
        metric_key=key,
        value=value,
        unit=str(d.get("unit", "") or "").strip(),
        period=str(d.get("period", "") or ""),
        cited_page=page,
        source=str(d.get("source", "llm")),
        raw=d,
    )


# ---------------------------------------------------------------------------
# 判定
# ---------------------------------------------------------------------------

TOL_EXACT = 0.005      # 0.5%
TOL_SCALE = 0.02       # 2%，容纳四舍五入（0.0718 vs 0.72 这类）
_SCALE_FACTORS = (10.0, 0.1, 100.0, 0.01, 1000.0, 1e-3, 1e4, 1e-4, 1e8, 1e-8)


def _rel(a: float, b: float) -> float:
    if b == 0:
        return 0.0 if a == 0 else float("inf")
    return abs(a - b) / abs(b)


def _fmt(v: float) -> str:
    if v == int(v) and abs(v) < 1e15:
        return f"{int(v):,}"
    return f"{v:,.4f}".rstrip("0").rstrip(".")


def _ev(page: PageRec, tok: Token | None, quote: str) -> dict:
    return {
        "source_id": page.source_id,
        "file_name": page.file_name,
        "source_type": page.source_type,
        "publication_date": page.publication_date,
        "page": page.page,
        "value": tok.value if tok else None,
        "unit": tok.unit if tok else "",
        "quote": quote,
    }


@dataclass
class Verdict:
    label: str
    error_type: str | None = None
    confidence: float = 0.0
    basis: str = ""
    suggested_fix: str | None = None
    unable_reason: str | None = None
    missing_input: str | None = None
    evidence: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "error_type": self.error_type,
            "confidence": self.confidence,
            "basis": self.basis,
            "suggested_fix": self.suggested_fix,
            "unable_reason": self.unable_reason,
            "missing_input": self.missing_input,
            "evidence": self.evidence,
        }


def _insufficient(reason: str, *, missing: str | None = None,
                  confidence: float = 0.3) -> Verdict:
    return Verdict(label=INSUFFICIENT, confidence=confidence, basis=reason,
                   unable_reason=reason, missing_input=missing)


def _recompute_dividend_amount(idx: MaterialIndex) -> tuple[float | None, list[dict], str]:
    """跨页勾稽：现金分红金额 = 股本基数 ÷ 10 × 每10股派息数。"""
    ev: list[dict] = []
    shares = per10 = None
    aliases = METRICS["total_shares"]["aliases"]
    for page in idx.alias_pages(aliases):
        for tok in tokens_near(page, aliases, window=40, base_unit="股"):
            if tok.value > 1e6 and "元" not in tok.after[:4]:
                shares = tok.value
                ev.append(_ev(page, tok, snippet(page, aliases[0])))
                break
        if shares:
            break
    aliases = METRICS["dividend_per_ten"]["aliases"]
    for page in idx.alias_pages(aliases):
        for tok in tokens_near(page, aliases, window=40, base_unit="元/10股"):
            if 0 < tok.value <= 10:
                per10 = tok.value
                ev.append(_ev(page, tok, snippet(page, aliases[0])))
                break
        if per10:
            break
    if not shares or per10 is None:
        return None, ev, "材料内未同时取到股本基数与每10股派息数，无法复算"
    return shares / 10.0 * per10, ev, f"{shares:,.0f} 股 ÷ 10 × {per10} 元 = {shares / 10.0 * per10:,.2f} 元"


def verify_claim(claim: Claim, idx: MaterialIndex) -> Verdict:
    """确定性判定。全部是代码，没有模型参与。"""
    if claim.metric_key is None:
        return _insufficient("未能把该主张归到可核验的财务指标上，无法定位对应披露值。")
    spec = METRICS[claim.metric_key]
    if claim.value is None:
        return _insufficient("该主张未给出具体数值，无法与披露值逐项对照。")

    base_unit = spec["unit"]
    v_base = to_base(claim.value, claim.unit, base_unit)
    if v_base is None:
        return _insufficient(
            f"主张单位「{claim.unit}」无法换算到「{base_unit}」，口径不明，无法比对。"
        )

    # 需要材料外输入（如股价）的指标：不猜，直接判证据不足
    if spec.get("rule") == "needs_external":
        return _insufficient(
            f"核验「{spec['cn']}」需要材料之外的价格类数据，当前材料集内不存在该来源。",
            missing="计算该指标所需的收盘价（或复权价）及其数据来源；以及所用股本/净利润口径说明。",
        )

    pages = idx.alias_pages(spec["aliases"])
    if not pages:
        return _insufficient(
            f"已纳入材料中未出现「{spec['cn']}」相关披露，无法核验该主张。"
        )

    # 现金分红：走跨页勾稽，而不是直接比字面值
    if spec.get("rule") == "dividend":
        expected, ev, how = _recompute_dividend_amount(idx)
        literal = []
        for page in pages:
            for tok in tokens_near(page, spec["aliases"], window=40, base_unit=spec["unit"]):
                if tok.value > 0:
                    literal.append((page, tok))
        if literal:
            ev = ev + [_ev(p, t, snippet(p, spec["aliases"][0])) for p, t in literal[:3]]
        if expected is None:
            return Verdict(label=INSUFFICIENT, confidence=0.3,
                           basis=f"无法复算：{how}", evidence=ev,
                           unable_reason=f"无法复算：{how}")
        if _rel(claim.value, expected) <= TOL_EXACT:
            return Verdict(label=SUPPORTED, confidence=0.9,
                           basis=f"按披露的股本基数与每10股派息数复算得 {expected:,.2f} 元，与主张一致。",
                           evidence=ev)
        return Verdict(
            label=CONTRADICTED, error_type="cross_reference", confidence=0.85,
            basis=(f"按材料披露复算：{how}，即 {expected:,.2f} 元；主张给出 {claim.value:,.2f} 元，"
                   f"两者相差约 {claim.value / expected:.1f} 倍。该主张与复算结果及材料内的分配总额"
                   f"表述不一致。"),
            suggested_fix=f"现金分红金额应为 {expected:,.2f} 元（约 {expected / 1e4:,.2f} 万元）。",
            evidence=ev,
        )

    # 一般数值比对：先在全部候选里找精确命中，再找量级/单位型偏差
    cands: list[tuple[PageRec, Token, float]] = []   # (页, 候选, 归一后的值)
    exact: tuple[PageRec, Token] | None = None
    for page in pages:
        for tok in tokens_near(page, spec["aliases"], base_unit=base_unit):
            t_base = to_base(tok.value, tok.unit or base_unit, base_unit)
            if t_base is None:
                continue
            cands.append((page, tok, t_base))
            if exact is None and _rel(t_base, v_base) <= TOL_EXACT:
                exact = (page, tok)

    def _dim_ok(t: Token) -> bool:
        u = t.unit
        if base_unit in ("%", "倍"):
            return u == base_unit
        if base_unit == "元/股":
            return u in ("", "元/股", "元")
        return u in ("", base_unit) or u in MONEY_SCALE

    def _quote(p: PageRec, t: Token | None = None) -> str:
        return snippet(p, spec["aliases"][0], value_raw=t.raw if t else None)

    # 口径核对要排在「精确命中」之前：口径错的主张，其数值本身往往是真的
    # （只是取自另一个口径的行），先判命中就会漏掉。
    if claim.metric_key == "weighted_roe" and cands:
        page, tok = exact or (cands[0][0], cands[0][1])
        says_parent = "归母" in claim.claim_text or "普通股股东" in claim.claim_text
        says_deducted = "扣非" in claim.claim_text or "扣除非经常性损益" in claim.claim_text
        if "扣除非经常性损益" in tok.before and says_parent and not says_deducted:
            others = [t for _p, t, _b in cands
                      if _dim_ok(t) and abs(t.value - tok.value) > 1e-9]
            ref = others[0] if others else None
            fix = (f"按归母净利润口径应为 {ref.value}%"
                   if ref else "按归母净利润口径应取未扣非的那一行")
            fix += "；若确要引用扣非口径，须在文中写明「扣除非经常性损益后」。"
            return Verdict(
                label=CONTRADICTED, error_type="caliber", confidence=0.8,
                basis=(f"主张写的是归母口径，但所引数值 {tok.value}% 出自材料中"
                       f"「{tok.before[-26:]}」所在行，属扣除非经常性损益后的口径，口径与数值不匹配。"),
                suggested_fix=fix,
                evidence=[_ev(page, tok, _quote(page, tok))],
            )

    if exact is not None:
        page, tok = exact
        if claim.cited_page and claim.cited_page != page.page:
            return Verdict(
                label=CONTRADICTED, error_type="citation_page", confidence=0.85,
                basis=(f"数值 {_fmt(tok.value)}{tok.unit} 本身正确，但主张把出处标为第 {claim.cited_page} 页；"
                       f"该值实际出现在第 {page.page} 页，第 {claim.cited_page} 页未出现该指标。"),
                suggested_fix=f"引用页码应改为第 {page.page} 页。",
                evidence=[_ev(page, tok, _quote(page, tok))],
            )
        return Verdict(label=SUPPORTED, confidence=0.9,
                       basis=f"材料第 {page.page} 页披露 {_fmt(tok.value)}"
                             f"{tok.unit or base_unit}，与主张一致。",
                       evidence=[_ev(page, tok, _quote(page, tok))])

    if not cands:
        return _insufficient(
            f"材料里出现了「{spec['cn']}」，但其披露位置附近未取到可用数值，无法比对。")

    # 量级/单位型偏差：找比值最贴近某个 10^k 的候选，而不是「相对差最小」的候选。
    # 只看同维度的候选——否则 32.5% 会被拿去和 12.4 亿 比，算出一个荒唐的倍数。
    scaled: tuple[PageRec, Token, float, float] | None = None
    for page, tok, t_base in cands:
        if not t_base or not _dim_ok(tok):
            continue
        ratio = v_base / t_base
        for f in _SCALE_FACTORS:
            err = abs(ratio - f) / f          # 相对倍数误差，小因子也不会被放宽
            if err <= TOL_SCALE and (scaled is None or err < scaled[3]):
                scaled = (page, tok, ratio, err)

    if scaled is not None:
        page, tok, ratio, _ = scaled
        material_unit = tok.unit or base_unit
        # 只有「钱对钱、数量对数量」的标签错才算单位错；元/股 与 元 的混用按量级差处理
        unit_label_mismatch = (claim.unit in MONEY_SCALE and material_unit in MONEY_SCALE
                               and claim.unit != material_unit)
        etype = "unit" if unit_label_mismatch else "numeric_scale"
        factor = f"约 {ratio:.2f} 倍" if ratio >= 1 else f"约 {1 / ratio:.1f} 分之一"
        return Verdict(
            label=CONTRADICTED, error_type=etype, confidence=0.8,
            basis=(f"主张 {claim.value}{claim.unit} 归一后为 {_fmt(v_base)}{base_unit}，"
                   f"与材料第 {page.page} 页披露的 {_fmt(tok.value)}{material_unit} 相差{factor}。"),
            suggested_fix=f"应改为 {_fmt(tok.value)}{material_unit}（出处：第 {page.page} 页）。",
            evidence=[_ev(page, tok, _quote(page, tok))],
        )

    same_dim = [c for c in cands if _dim_ok(c[1]) and (base_unit != "%" or c[1].value >= 0)]
    shown = same_dim[:4] or cands[:1]
    page, tok, _ = shown[0]
    return Verdict(
        label=CONTRADICTED, error_type="numeric_mismatch", confidence=0.75,
        basis=(f"主张 {claim.value}{claim.unit}（归一 {_fmt(v_base)}{base_unit}）与材料第 {page.page} 页"
               f"披露的 {_fmt(tok.value)}{tok.unit or base_unit} 不符；该页同类披露值为 "
               + "、".join(f"{_fmt(t.value)}{t.unit or base_unit}" for _p, t, _b in shown) + "。"),
        suggested_fix=f"应核对并改为材料披露值（如 {_fmt(tok.value)}{tok.unit or base_unit}，"
                      f"出处：第 {page.page} 页）。",
        evidence=[_ev(p, t, _quote(p, t)) for p, t, _b in shown],
    )


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


@dataclass
class DraftReviewResult:
    run_log_id: str
    case_id: str
    company: str
    cutoff: str
    claims: list[dict]
    excluded: list[dict]
    warnings: list[str]
    stages: dict[str, Any]
    markdown: str = ""

    def summary(self) -> dict[str, int]:
        out = {SUPPORTED: 0, CONTRADICTED: 0, INSUFFICIENT: 0}
        for c in self.claims:
            out[c["verdict"]["label"]] = out.get(c["verdict"]["label"], 0) + 1
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_log_id": self.run_log_id,
            "case_id": self.case_id,
            "company": self.company,
            "prediction_cutoff_date": self.cutoff,
            "claims": self.claims,
            "excluded_documents": self.excluded,
            "warnings": self.warnings,
            "stages": self.stages,
            "summary": self.summary(),
        }


def _run_log_id(payload: dict) -> str:
    import hashlib

    h = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:12]
    return f"RUN-{h}"


def run_draft_case(payload: dict, *, use_llm: bool = True, cfg: dict | None = None) -> DraftReviewResult:
    """跑完整闭环。payload 见 data/annotations/cases/*.json。"""
    case_id = payload.get("case_id", "case")
    company = payload.get("company", "")
    cutoff = payload["prediction_cutoff_date"]
    draft_path = ROOT / payload["draft"]
    stages: dict[str, Any] = {}
    warnings: list[str] = []

    stages["stage0_run_log_id"] = _run_log_id(payload)

    # ① 主张拆解
    draft_text = draft_path.read_text(encoding="utf-8")
    raw_claims, how, degrade = split_claims(draft_text, cfg=cfg, use_llm=use_llm, source=str(draft_path.name))
    if degrade:
        warnings.append(degrade)
    stages["stage1_decomposition"] = {"method": how, "count": len(raw_claims)}
    claims = [_to_claim(c, i + 1) for i, c in enumerate(raw_claims)]

    # ② 材料登记 + 截止日过滤（硬门）
    sources, warn = dr.recognize_all(payload.get("documents", []))
    warnings.extend(warn)
    allowed, excluded, page_recs = [], [], []
    for sd in sources:
        ref = cf.EvidenceRef if hasattr(cf, "EvidenceRef") else None  # noqa: F841
        from .models import ClaimRecord, EvidenceRef

        rec = ClaimRecord(
            claim_id=sd.source_id, claim_text="", subject=company, period="",
            evidence=[EvidenceRef(source_id=sd.source_id, file_name=sd.file_name,
                                  source_type=sd.source_type,
                                  publication_date=sd.publication_date)],
        )
        verdict, reasons = cf.filter_record(rec, cutoff)
        if verdict == cf.ELIGIBLE:
            allowed.append(sd)
        else:
            excluded.append({"source_id": sd.source_id, "file_name": sd.file_name,
                             "publication_date": sd.publication_date, "reasons": reasons})
            warnings.append(f"材料 {sd.file_name}（{sd.publication_date}）晚于截止日 {cutoff}，已排除")
    stages["stage2_materials"] = {
        "allowed": [s.file_name for s in allowed],
        "excluded": [e["file_name"] for e in excluded],
    }
    for sd in allowed:
        path = ROOT / sd.path if sd.path else None
        if path and path.exists():
            page_recs.extend(load_pdf_pages(path, sd))
        else:
            warnings.append(f"{sd.file_name}: 找不到本地文件（{sd.path}），该材料未参与检索")
    idx = MaterialIndex(page_recs)
    stages["stage3_indexed_pages"] = len(page_recs)

    # ③④⑤ 检索 → 判定 → 标签
    out: list[dict] = []
    for c in claims:
        v = verify_claim(c, idx)
        wg.assert_clean([c.claim_text], mode="pre_hoc", has_confirmation=False, label=(
            "RISK_SIGNAL_DETECTED" if v.label == CONTRADICTED else "INSUFFICIENT_INFORMATION"))
        out.append({
            "claim_id": c.claim_id,
            "claim_text": c.claim_text,
            "metric": METRICS[c.metric_key]["cn"] if c.metric_key else None,
            "metric_key": c.metric_key,
            "claim_value": c.value,
            "claim_unit": c.unit,
            "cited_page": c.cited_page,
            "decomposed_by": c.source,
            "verdict": v.to_dict(),
        })
    stages["stage5_verdicts"] = {c["claim_id"]: c["verdict"]["label"] for c in out}

    res = DraftReviewResult(
        run_log_id=stages["stage0_run_log_id"], case_id=case_id, company=company,
        cutoff=cutoff, claims=out, excluded=excluded, warnings=warnings, stages=stages,
    )
    res.markdown = render_markdown(res)
    return res


def render_markdown(res: DraftReviewResult) -> str:
    s = res.summary()
    lines = [
        f"# 研报草稿核查报告　{res.company or res.case_id}",
        "",
        f"- 运行日志编号：{res.run_log_id}",
        f"- 预测截止日：{res.cutoff}",
        f"- 主张合计：{len(res.claims)} 条　支持 {s[SUPPORTED]} 条 / 矛盾 {s[CONTRADICTED]} 条 / "
        f"证据不足 {s[INSUFFICIENT]} 条",
        "",
    ]
    if res.excluded:
        lines += ["## 被时间过滤排除的材料", ""]
        lines += [f"- {e['file_name']}（{e['publication_date']}）：{'；'.join(e['reasons'])}"
                  for e in res.excluded]
        lines.append("")
    lines += ["## 逐条核查", ""]
    for c in res.claims:
        v = c["verdict"]
        lines.append(f"### {c['claim_id']}　[{v['label']}]"
                     + (f"　错误类型：{v['error_type']}" if v["error_type"] else ""))
        lines.append(f"- 主张：{c['claim_text']}")
        lines.append(f"- 判定依据：{v['basis']}")
        if v.get("suggested_fix"):
            lines.append(f"- 修改建议：{v['suggested_fix']}")
        if v.get("unable_reason"):
            lines.append(f"- 无法判断原因：{v['unable_reason']}")
        if v.get("missing_input"):
            lines.append(f"- 缺失输入：{v['missing_input']}")
        for e in v.get("evidence", [])[:4]:
            page = f"第 {e['page']} 页" if e.get("page") else "页码未取到"
            lines.append(f"- 证据：{e['file_name']} {page}：{e['quote']}")
        lines.append("")
    if res.warnings:
        lines += ["## 提示", ""]
        lines += [f"- {w}" for w in res.warnings]
        lines.append("")
    lines.append("> 本报告由确定性规则生成，数值判定不经过大模型。所有结论均可回溯到披露原文页码。")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 评测
# ---------------------------------------------------------------------------


def _parse_num(s: Any) -> float | None:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    m = re.search(r"-?\d[\d,]*(?:\.\d+)?", str(s))
    return _to_float(m.group(0)) if m else None


def evaluate(result: DraftReviewResult, expected_path: str | Path) -> dict[str, Any]:
    """把核查结果与真值标注对照，算案例级指标。

    匹配口径是「指标 + 数值」，不是 claim_id —— 拆解方式不同（模型 vs 规则）
    条数和编号都不会一致，按编号对会把拆解差异误记成判定错误。
    """
    exp = json.loads(Path(expected_path).read_text(encoding="utf-8"))
    gold = exp["claims"]
    got = result.claims
    used: set[int] = set()
    pairs: list[tuple[dict, dict | None]] = []
    for g in gold:
        gkey = g.get("metric")
        gkey = METRIC_KEY_ALIASES.get(gkey, gkey)
        if gkey not in METRICS:
            gkey = resolve_metric(str(g.get("claim_text") or ""))
        gval = _parse_num(g.get("value"))
        gbase = gval
        if gval is not None and gkey in METRICS:
            conv = to_base(gval, str(g.get("unit", "") or ""), METRICS[gkey]["unit"])
            if conv is not None:
                gbase = conv
        pick: tuple[float, int] | None = None
        for i, c in enumerate(got):
            if i in used:
                continue
            ckey = c.get("metric_key") or resolve_metric(str(c.get("metric") or ""))
            if ckey != gkey:
                continue
            cval = c.get("claim_value")
            if gbase is None or cval is None:
                d = 0.0
            else:
                cbase = cval
                if ckey in METRICS:
                    conv = to_base(cval, str(c.get("claim_unit") or ""), METRICS[ckey]["unit"])
                    if conv is not None:
                        cbase = conv
                d = _rel(cbase, gbase)
                if d > 0.03:
                    continue
            if pick is None or d < pick[0]:
                pick = (d, i)
        if pick:
            used.add(pick[1])
            pairs.append((g, got[pick[1]]))
        else:
            pairs.append((g, None))

    tp = fp = fn = tn = 0
    page_ok = page_tot = 0
    rows = []
    for g, c in pairs:
        want = g["expected_verdict"]
        want_err = bool(g.get("expected_flag_as_error"))
        if c is None:
            have, etype = None, None
            if want_err:
                fn += 1
            else:
                tn += 1
        else:
            have = c["verdict"]["label"]
            etype = c["verdict"]["error_type"]
            have_err = have == CONTRADICTED
            if want_err and have_err:
                tp += 1
            elif want_err and not have_err:
                fn += 1
            elif not want_err and have_err:
                fp += 1
            else:
                tn += 1
            if want_err and have_err:
                gold_pages = {e["pdf_page"] for e in g.get("evidence", [])
                              if isinstance(e, dict) and e.get("pdf_page")}
                got_pages = {e["page"] for e in c["verdict"].get("evidence", []) if e.get("page")}
                if gold_pages:
                    page_tot += 1
                    if gold_pages & got_pages:
                        page_ok += 1
        rows.append({
            "claim_id": g["claim_id"],
            "metric": g.get("metric"),
            "matched_claim": c["claim_id"] if c else None,
            "expected": want,
            "got": have,
            "match": have == want,
            "expected_error_type": g.get("error_type"),
            "got_error_type": etype,
        })

    covered = sum(1 for _, c in pairs if c is not None)
    n = len(rows)
    acc = sum(1 for r in rows if r["match"]) / n if n else 0.0
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    return {
        "scope": "case_level",
        "case_id": result.case_id,
        "gold_claims": n,
        "matched_claims": covered,
        "coverage": round(covered / n, 4) if n else 0.0,
        "runtime_claims": len(got),
        "label_accuracy": round(acc, 4),
        "error_detection": {
            "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": round(prec, 4),
            "recall": round(rec, 4),
            "false_positive_rate": round(fp / (fp + tn), 4) if (fp + tn) else 0.0,
        },
        "page_localization_accuracy": round(page_ok / page_tot, 4) if page_tot else None,
        "rows": rows,
        "note": "样本量小，仅案例级；不得外推为泛化准确率。",
    }
