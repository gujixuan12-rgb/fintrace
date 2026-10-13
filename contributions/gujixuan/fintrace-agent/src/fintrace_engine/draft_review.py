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

import calendar
import json
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from . import llm
from .calculators.consistency import expected_dividend_yuan
from .models import SourceDocument, parse_date
from .parsers import document_recognizer as dr
from .verification import cutoff_filter as cf
from .verification import wording_guard as wg

ROOT = Path(__file__).resolve().parents[2]

SUPPORTED = "SUPPORTED"
CONTRADICTED = "CONTRADICTED"
INSUFFICIENT = "INSUFFICIENT"
# 第四类：预测/观点（非披露事实）。单列一行、不进核查分母。
PREDICTION = "PREDICTION"

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
    "dividend_per_ten": {"cn": "每10股派息数",
                         "aliases": ["每10股派息数", "每10股派发现金红利", "每10股派息",
                                     "每10股派发现金股利", "10股派发现金红利"],
                         "unit": "元/10股"},
    "distributable_profit": {"cn": "可分配利润", "aliases": ["可分配利润"], "unit": "元"},
    "pe_ratio": {"cn": "市盈率", "aliases": ["市盈率"], "unit": "倍", "rule": "needs_external"},
    "net_profit_deducted": {
        "cn": "扣除非经常性损益后的归属于上市公司股东的净利润",
        "aliases": ["归属于上市公司股东的扣除非经常性损益的净利润",
                    "归属于上市公司股东的扣除非经常性损益后的净利润",
                    "扣除非经常性损益后归属于上市公司股东的净利润",
                    "扣除非经常性损益后归属于公司普通股股东的净利润",
                    "扣非归母净利润", "扣非净利润",
                    # 研报行文里的简写（模型拆主张时很常这么写）：
                    "扣非后归母净利润", "扣非后净利润",
                    "扣非后归属于上市公司股东的净利润",
                    "扣非后归属于公司普通股股东的净利润"],
        "unit": "元"},
}

#: 真值标注里用过的等价指标名，评测时归一到引擎的指标键。
METRIC_KEY_ALIASES = {
    "net_profit_deducted_nonrecurring": "net_profit_deducted",
    "net_profit_attributable_to_parent": "net_profit_parent",
    "net_assets_attributable_to_parent": "net_assets_parent",
    "operating_cash_flow_net": "operating_cash_flow",
    "weighted_avg_roe": "weighted_roe",
    "cash_dividend_amount": "cash_dividend",
    "basic_eps_with_citation": "basic_eps",
}

#: 单位 → 以该指标基准单位计的倍数。「元」类指标的基准单位是元。
MONEY_SCALE = {"元": 1.0, "千元": 1e3, "万元": 1e4, "百万元": 1e6, "亿元": 1e8}
COUNT_SCALE = {"股": 1.0, "万股": 1e4, "亿股": 1e8}
PASSTHROUGH_UNITS = {"%", "个百分点", "倍", "元/股", "元/10股"}
#: 比率主张遇到「材料那格没标单位」时，数值形如比率的门槛（超过就不是同一口径了）
_RATIO_PLAUSIBLE_MAX = 1000.0

_MONEY_UNITS = tuple(MONEY_SCALE)

#: 数值后面紧跟单位的识别（长单位要排在短单位前面：百万元 先于 万元、千元）
_UNIT_AFTER = re.compile(r"^(个百分点|元/股|元/10股|百万元|万元|亿元|千元|万股|亿股|元|%|股|倍)")
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
#: （`1,243,188,170.211,541,122,075.60`），按精度读才分得开。
#: 「元/10股」必须给到 3 位：每10股派息数常见 0.155 这种三位小数，读成 2 位
#: （0.15）会让分红金额复算差 90 万元，并把错误值写进修正建议；给到 4 位则会把
#: 相邻单元格（`0.155  1.23` 去空白后成 `0.1551.23`）粘进来，所以是 3 不是 4。
PRECISION = {"元": 2, "股": 0, "元/股": 4, "%": 2, "倍": 2, "元/10股": 3}


def _round_decimals(tok: str, digits: int) -> str:
    """按指标精度**四舍五入**（不是截断），并保留原写法里的千分位。

    截断会造出文档里根本不存在的数：「408,548.455」被截成 408,548.45，报告里就写出一个
    查不到出处的值。规则统一为 `Decimal` + `ROUND_HALF_UP`，与 `calculators/consistency.py`
    用的是同一套（后者负责跨页勾稽的算术）。
    本来没有小数位的整数原样返回：补成「2023.00」会把年份当成数值。
    """
    sep = "," in tok
    body = tok.replace(",", "")
    if "." not in body:
        return tok
    try:
        num = Decimal(body)
    except (InvalidOperation, ValueError):
        return tok
    if not num.is_finite():
        return tok
    keep = min(digits, len(body.split(".", 1)[1]))
    q = num.quantize(Decimal(1).scaleb(-keep), rounding=ROUND_HALF_UP)
    return f"{q:,f}" if sep else f"{q:f}"


#: 真正的千分位逗号：后面正好三位数字才算（「今日，公司」这类中文逗号不动）
_THOUSANDS_RX = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")


def _norm_seps(text: str) -> str:
    """去千分位逗号、把各种减号归一成 '-'，其余原样（保留符号与数字边界）。"""
    return _THOUSANDS_RX.sub("", (text or "").replace("−", "-").replace("－", "-"))


def num_in_text(page_text: str, num: str) -> bool:
    """页文本里有没有这个数：数字一位不差、两端有边界、正负号必须对得上。

    这是**唯一**的「数值回查原件」实现，引擎与评测共用：报告不能引一个在那一页上
    指不出来的数，评测也不能用更宽的口径去「验证」它。
    只容忍两件不算「不同的数」的事：数字被拆行（`1243188,17` + `0.21`）、末尾补零
    （页面写 0.10、值解析成 0.1）。数字本身与符号一律不放宽：12.7 不认 12.74，
    18.02 不认 18.99 / 118.02，正数不认负数。
    """
    n = _norm_seps(str(num)).strip()
    if not n:
        return False
    body = r"[\s,]*".join(re.escape(ch) for ch in n if ch.isdigit() or ch in ".-")
    if not body:
        return False
    tail = r"0*(?![\d.])" if "." in n else r"(?:\.0*)?(?![\d.])"
    left = r"(?<![\d.])" if n.startswith("-") else r"(?<![\d.\-])"
    return re.search(rf"{left}{body}{tail}", _norm_seps(page_text)) is not None


def value_spellings(value: Any) -> list[str]:
    """同一个数的等价写法（数值不变、**小数位不变**）：带千分位与不带两种。

    小数位取该值自己的写法（0.155 三位、18.02 两位），整数值不带小数点。
    不做取整候选：那会让 18.02 的「18」把页面上的 18.99、118 全命中。
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return []
    if v != v or v in (float("inf"), float("-inf")):
        return []
    txt = repr(v)
    if "e" in txt or "E" in txt:                 # 科学计数法：转定点再数小数位
        txt = f"{v:.10f}".rstrip("0").rstrip(".")
    dec = 0
    if "." in txt and not txt.endswith(".0"):
        dec = min(len(txt.split(".")[1]), 6)
    return sorted({f"{v:.{dec}f}", f"{v:,.{dec}f}"})


def page_has_value(page_text: str, value: Any) -> bool:
    """这一页上有没有这个值（回查原件的唯一入口）。"""
    return any(num_in_text(page_text, s) for s in value_spellings(value))


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
    # 「主要会计数据和财务指标」这类表的网格（pymupdf find_tables）；没有就是空。
    tables: tuple[tuple[tuple[str, ...], ...], ...] = ()

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
    out: list[PageRec] = []
    for i in range(doc.page_count):
        raw = doc[i].get_text()
        out.append(
            PageRec(
                source_id=source.source_id,
                file_name=source.file_name,
                source_type=source.source_type,
                publication_date=source.publication_date,
                page=i + 1,
                raw=raw,
                # 只对可能含摘要表的页读表格网格（find_tables 不便宜）
                tables=_page_tables(doc[i]) if _TABLE_MARK.search(raw) else (),
            )
        )
    doc.close()
    return out


_TABLE_MARK = re.compile(r"主要会计数据|主要财务指标|主要会计数据和财务指标")


def _page_tables(page) -> tuple[tuple[tuple[str, ...], ...], ...]:
    """页上的表格网格；pymupdf 读不出来就返回空（判定层退回邻域取候选）。"""
    try:
        tabs = page.find_tables()
    except Exception:  # pragma: no cover - 表格识别失败不该中断核查
        return ()
    out = []
    for tab in getattr(tabs, "tables", []) or []:
        try:
            grid = tab.extract()
        except Exception:  # pragma: no cover
            continue
        out.append(tuple(tuple("" if c is None else str(c) for c in row) for row in grid))
    return tuple(out)


_WS_RX = re.compile(r"\s")


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


@lru_cache(maxsize=32)
def _flat_map(raw: str) -> tuple[str, tuple[int, ...]]:
    """flat 串，以及 flat 每个字符在原文里的下标（用于把匹配位置映射回原文）。"""
    chars: list[str] = []
    idx: list[int] = []
    for i, ch in enumerate(raw):
        if _WS_RX.match(ch):
            continue
        chars.append(ch)
        idx.append(i)
    return "".join(chars), tuple(idx)


def _appears_in_raw(page: PageRec, raw: str, at: int) -> bool:
    """候选值必须真的以这个形态出现在原文里（at 是它在 flat 里的起始下标）。

    去空白（flat）是为了接上被换行截断的数字，副作用是把上一列/附注序号粘进来：
    原文 `附注 / 十九、4 / 81,420,154.58` 去空白后成了 `十九、481,420,154.58`，
    凭空多出 10 倍假值，再拿去当「单位写错」的证据就是误报。

    断行的判法：数字内部允许空白，但只允许两种断法——
      ① 千分位逗号之后（`1,243,\n188,170.21`）；
      ② 断点所在那一行本身是纯数字行（`1,243,188,17\n0.21`）。
    「十九、4」不是纯数字行，所以它后面接的数字不算同一个数，假值就此被挡住。
    """
    text, mapping = _flat_map(page.raw)
    if text[at:at + len(raw)] != raw:
        return False
    lo, hi = mapping[at], mapping[at + len(raw) - 1]
    for m in re.finditer(r"\s+", page.raw[lo:hi + 1]):
        j = lo + m.start()
        if page.raw[j - 1:j] == ",":
            continue
        line_start = page.raw.rfind("\n", 0, j) + 1
        line = page.raw[line_start:j].strip()
        if line and all(c in "0123456789,.-" for c in line):
            continue
        return False
    return True


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
                if not _appears_in_raw(page, m.group(0), start + m.start()):
                    continue
                # 读数字要越过窗口边界：窗口正好切在数字中间时（`12.74%` 只看到 `12.7`）
                # 会造出一个页面上不存在的数。往后把还连着的数字读齐，再按精度四舍五入
                # —— 多读的那几位本来就在精度之外，舍入后与按原样读一致。
                text_raw = m.group(0)
                j = start + m.start() + len(text_raw)
                extra = 0
                while j < len(text) and extra < 6 and (
                        text[j].isdigit() or (text[j] == "." and "." not in text_raw)):
                    text_raw += text[j]
                    j += 1
                    extra += 1
                if extra and not _appears_in_raw(page, text_raw, start + m.start()):
                    continue
                raw = _round_decimals(text_raw, prec)
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


#: 净资产收益率表的「扣非」行标签（原件里常被拆成两三段，网格读不出来）
_ROE_DEDUCT_LABEL = "扣除非经常性损益"


def _dividend_literal_ok(tok: Token) -> bool:
    """这一条能不能当「现金分红金额」的字面披露值。

    邻域取候选会把同一行/相邻行里别的数字也捡进来（实测捡到过「5」和别的表的
    「40,854,845」），它们不是分红金额。判据是「数字紧跟在指标标签后面」：`before`
    正是标签到数字之间那段文本，它要短、且中间不能再出现别的标签或分句。
    """
    gap = tok.before.strip()
    if len(gap) > 14 or re.search(r"[，,、；;。]", gap):
        return False
    return tok.unit in ("", "元") or tok.unit in MONEY_SCALE


def _deducted_row_token(page: PageRec, *, claim_value: float | None,
                        base_unit: str = "%") -> Token | None:
    """整页文本里找「扣除非经常性损益」那一行的数值（网格/列都绑不出来时的兜底）。

    净资产收益率表常把行标签拆成几段（「扣除非经常性损益后归属于 / 公司普通股股东的净利」），
    pymupdf 的表格网格读不出来，行列绑定也就无从谈起。但主张值本身能指认它取自哪一行：
    值等于扣非行那一列的数，就是口径混用（天下秀 2023 年报第 268 页就是这个形态）。
    """
    text = page.flat
    prec = PRECISION.get(base_unit, 2)
    rx = _number_re(prec)
    for m in re.finditer(_ROE_DEDUCT_LABEL, text):
        tail = text[m.end():m.end() + 48]
        for tm in rx.finditer(tail):
            raw = _round_decimals(tm.group(0), prec)
            val = _to_float(raw)
            if val is None or _is_year_token(raw, val):
                continue
            if claim_value is not None and _rel(val, claim_value) <= TOL_EXACT:
                return Token(value=val, unit="", base=None, raw=raw, page=page.page,
                             before=_ROE_DEDUCT_LABEL + tail[:tm.start()],
                             after=tail[tm.end():tm.end() + 40])
            break          # 只看紧邻标签的第一个数（那一行的首列）
    return None


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
    assertion: str = ""
    source_sentence_id: str = ""
    claim_group_id: str = ""
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
        # cn 也算别名：指标表里的显示名必须能自己解析回本键。否则
        # 「扣除非经常性损益后的归属于上市公司股东的净利润」会因表内没有同名别名，
        # 被截尾匹配成「归属于上市公司股东的净利润」，扣非主张就落到了归母口径上。
        for a in [*spec["aliases"], spec["cn"]]:
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

#: PDF 转文本常把一句话按固定宽度硬折行，「2026 年半年\n度报告」这种断词换行按换行断句
#: 就会切出「度报告，1H26 实现营业收入…」的半截主张。只接「两边都是字」的换行；
#: 句末换行（左侧是「。」）与段间空行不接。
_SOFT_WRAP_RX = re.compile(
    r"(?<=[\u4e00-\u9fff0-9A-Za-z%）)])\n[ \t]*(?=[\u4e00-\u9fff0-9A-Za-z（(])")


def _unwrap_soft_wraps(text: str) -> str:
    """把硬折行接回去，接完再断句。"""
    prev = None
    while prev != text:
        prev = text
        text = _SOFT_WRAP_RX.sub("", text)
    return text


def _sentence_span(text: str, pos: int) -> tuple[int, int]:
    """返回 pos 所在句子的 [起, 止) 区间（按 _SENT_SPLIT 断句）。"""
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
    return lo, hi


def _sentence_of(text: str, pos: int) -> str:
    lo, hi = _sentence_span(text, pos)
    return text[lo:hi].strip()


def _sentence_index(text: str) -> list[tuple[int, int]]:
    """全文句子区间表，用来给主张挂 source_sentence_id。"""
    spans: list[tuple[int, int]] = []
    lo = 0
    for m in _SENT_SPLIT.finditer(text):
        if text[lo:m.start()].strip():
            spans.append((lo, m.start()))
        lo = m.end()
    if text[lo:].strip():
        spans.append((lo, len(text)))
    return spans


def _sid_of(spans: list[tuple[int, int]], pos: int) -> str:
    """pos 落在第几句（1 起），返回 "S03" 这样的句子编号。"""
    for i, (lo, hi) in enumerate(spans, 1):
        if lo <= pos < hi:
            return f"S{i:02d}"
    return "S??"


# --- 子句：断言的最小单位 ---------------------------------------------------
#
# 一句话里可以既有事实又有预测、既有本期又有去年：「公司 2023 年营业收入 42.02 亿元，
# 预计 2024 年营业收入 45.20 亿元」。按整句记账会同时出两个错 —— 预测句盖住事实句
# （实际值被归为「预测」），或者后半句被当成前半句的重复而丢掉。所以断言取到**子句**一级：
# 子句边界 = 句读 + 预测用语起点。
_CLAUSE_CUT_RX = re.compile(r"[，,；;。！？!?]")
_FORECAST_BOUNDARY_RX = re.compile(r"预计|预测|料将|有望|目标价|合理价值|盈利预测|我们预计|上调|下调")


def _clause_cuts(text: str) -> list[int]:
    cuts = {0, len(text)}
    for m in _CLAUSE_CUT_RX.finditer(text):
        i, j = m.start(), m.end()
        if text[i] in "，," and i > 0 and j < len(text) \
                and text[i - 1].isdigit() and text[j].isdigit():
            continue               # 千分位逗号（25,097.45）不是子句边界
        cuts.add(i)
        cuts.add(j)
    for m in _FORECAST_BOUNDARY_RX.finditer(text):
        cuts.add(m.start())        # 预测用语起新子句，且自己归入后一个子句
    return sorted(cuts)


def _clause_span(text: str, pos: int, *, cuts: list[int] | None = None) -> tuple[int, int]:
    """pos 落在哪个子句里。"""
    cuts = _clause_cuts(text) if cuts is None else cuts
    for a, b in zip(cuts, cuts[1:]):
        if a <= pos < b:
            return a, b
    return 0, len(text)


def _value_variants(value: float | None) -> list[str]:
    """一个数在正文里可能的写法（42.02 / 42.020 / 4,202,030,347.42）。"""
    if value is None:
        return []
    out = {f"{value:g}", f"{value:,.2f}", f"{value:,.0f}", f"{value:.2f}", f"{value:.0f}",
           f"{value:,.4f}".rstrip("0").rstrip(".")}
    return sorted((p for p in out if p), key=len, reverse=True)


def _value_pos_in(text: str, claim: "Claim") -> int | None:
    """主张自己那个数值在它文本里的位置；找不到返回 None。"""
    for pat in _value_variants(claim.value):
        i = (text or "").find(pat)
        if i >= 0:
            return i
    return None


#: 口径用语：同句里的「归母」与「扣非」是两条不同断言，不能互相去重。
_CALIBER_RX: list[tuple[str, re.Pattern[str]]] = [
    ("deducted", re.compile(r"扣除非经常性损益|扣非")),
    ("parent", re.compile(r"归母|归属于上市公司股东|归属于母公司")),
]


def _claim_caliber(text: str) -> str:
    for name, rx in _CALIBER_RX:
        if rx.search(text or ""):
            return name
    return ""


def _claim_relation(metric_key: str | None, unit: str) -> str:
    """水平值还是增减率：单位是 % 而指标基准不是 % 的，是同比/环比增减率。"""
    if unit != "%" or not metric_key:
        return "level"
    return "level" if METRICS.get(metric_key, {}).get("unit") == "%" else "yoy"


def _text_period_kind(text: str) -> str:
    """子句里写明的期间，归一成 2026A / 2026H1 / 2026Q3；没写就返回空串。

    写明的期间要进断言身份：同一句里「2026 年上半年」与「2026 年全年」是两条断言，
    「2023 年」与「2022 年」也是 —— 只按「指标 + 值」去重会把其中一条吃掉。
    """
    t = text or ""
    m = re.search(r"(?<!\d)(20\d{2})\s*年?\s*(上|下)?半年", t)
    if m:
        return f"{m.group(1)}H{'2' if m.group(2) == '下' else '1'}"
    m = re.search(r"(?<!\d)(20\d{2})\s*年?\s*[Qq]([1-4])(?!\d)", t)
    if m:
        return f"{m.group(1)}Q{m.group(2)}"
    m = re.search(r"(?<!\d)([1-4])\s*[Qq]\s*(20\d{2})(?!\d)", t)
    if m:
        return f"{m.group(2)}Q{m.group(1)}"
    m = re.search(r"(?<!\d)(20\d{2})\s*年?(?!\d)", t)
    return f"{m.group(1)}A" if m else ""



def _locate_sentence(claim: dict, flat: list[str]) -> int | None:
    """把一条主张定位到草稿里的句子，返回句号（1 起）；定不了返回 None。

    先按原句片段逐字找（规则抽取与抄句子的模型主张走这条）；
    模型改写过的断言不在草稿里逐字出现，回退为「指标名 + 数值同句出现」——
    数值按词边界匹配，避免「7%」被「7.15%」里的 7 命中；多句都命中就不猜。
    """
    probe = re.sub(r"\s+", "", str(claim.get("claim_text") or ""))[:12]
    if probe:
        for i, s in enumerate(flat, 1):
            if probe in s:
                return i
    key = resolve_metric(str(claim.get("metric") or "")) or resolve_metric(str(claim.get("claim_text") or ""))
    val = claim.get("value")
    if key is None or val in (None, ""):
        return None
    num = _to_float(str(val))
    if num is None:
        return None
    forms = {str(val), f"{num:g}", f"{num:,.0f}", f"{num:,.1f}", f"{num:,.2f}"}
    pats = [re.compile(rf"(?<![\d.]){re.escape(f)}(?![\d.])") for f in forms if f]
    # 指标名要用全部别名去找：草稿写「归母净利润」而 cn 是「归属于上市公司股东的净利润」，
    # 只比 cn 会一条都定位不到。
    names = METRICS[key].get("aliases") or [METRICS[key]["cn"]]
    hits = [i for i, s in enumerate(flat, 1)
            if any(n in s for n in names) and any(p.search(s) for p in pats)]
    return hits[0] if len(hits) == 1 else None


def _attach_sentence_ids(claims: list[dict], draft_text: str) -> None:
    """给模型拆出来的主张补 source_sentence_id / claim_group_id。

    定位不到就留空 —— 不猜，也不因此丢掉主张。
    """
    spans = _sentence_index(draft_text)
    flat = [re.sub(r"\s+", "", draft_text[lo:hi]) for lo, hi in spans]
    for c in claims:
        if c.get("source_sentence_id"):
            continue
        i = _locate_sentence(c, flat)
        if i is None:
            continue
        c["source_sentence_id"] = f"S{i:02d}"
        c["claim_group_id"] = f"G{i:02d}"


_FALLBACK = re.compile(r"(-?\d[\d,]*(?:\.\d+)?)\s*(亿元|万元|万股|亿股|元/股|元|%|倍)")


def _assertion_text(key: str, value: str, unit: str) -> str:
    """把「指标 + 值 + 单位」写成一条能独立读懂的断言。

    单位是 % 而指标基准单位不是 % 的，是同比/环比增减率（摘要表「本期比上年同期增减」
    那一列），不能写成「营业收入 53%」——那看起来像营收只有 53 元。
    """
    if unit == "%" and METRICS[key]["unit"] != "%":
        return f"{METRICS[key]['cn']}同比增减 {value}%"
    return f"{METRICS[key]['cn']} {value}{unit}"


def extract_claims_rules(draft_text: str) -> list[dict]:
    """不给大模型也能跑：按「数值+单位」定位，再向左找指标名。

    拆得比模型粗（同一句里多个指标可能配错），但保证确定性链路可离线复现。
    断言按**指标 + 期间 + 口径 + 关系 + 值**识别，粒度到子句：同一句里「本期实际值」
    与「预计次年值」、「2023 年」与「2022 年」、「归母」与「扣非」各算一条；
    同一子句里的「同比增减%」仍是水平值那条的一部分，不另计条。
    每条都带 assertion、source_sentence_id、claim_group_id 与草稿定位（source_span）。
    """
    text = _unwrap_soft_wraps(draft_text)
    spans = _sentence_index(text)
    cuts = _clause_cuts(text)
    cands: list[dict] = []
    for m in _FALLBACK.finditer(text):
        ctx = text[max(0, m.start() - 40):m.start()]
        key = resolve_metric(ctx, len(ctx))
        if key is None:
            continue
        lo, hi = _clause_span(text, m.start(), cuts=cuts)
        clause = text[lo:hi].strip()
        sid = _sid_of(spans, m.start())
        sent = _sentence_of(text, m.start())
        page = None
        pm = None
        for pm in re.finditer(r"第\s*(\d+)\s*页", sent):
            pass
        if pm:
            page = int(pm.group(1))
        cands.append({
            "claim_text": clause,
            "source_sentence": sent,
            "source_span": [lo, hi],
            "assertion": _assertion_text(key, m.group(1), m.group(2)),
            "metric": METRICS[key]["cn"],
            "metric_key": key,
            "value": m.group(1),
            "unit": m.group(2),
            "page": page,
            "source_sentence_id": sid,
            "claim_group_id": sid.replace("S", "G"),
            # 口径按整句判：「…净利润 0.74 万元，同比下降 28.96%」里后一个子句也属同一口径，
            # 按子句判会让「同比增减%」脱离它所属的那条断言（同句同口径的后半句要能合并）。
            "caliber": _claim_caliber(sent),
            "relation": _claim_relation(key, m.group(2)),
            # 期间按子句判：同一句里「2023 年」与「2022 年」是两条断言，不能互相吃掉
            "period_kind": _text_period_kind(clause),
            "is_forecast": bool(_FORECAST_BOUNDARY_RX.search(clause)),
            # 合并用的键：同句同指标同口径，「同比增减%」归到水平值那条
            "_collapse": (key, _text_period_kind(sent), _claim_caliber(sent)),
            "source": "rules",
        })
    # 水平值先登记：同一句里的「同比增减%」不再单独计条（它是那条断言的一部分）
    levels = {c["_collapse"] for c in cands if c["relation"] == "level"}
    out: list[dict] = []
    seen: set[tuple] = set()
    for c in cands:
        c.pop("_collapse", None)
        ident = tuple(c[k] for k in _IDENT_KEYS)
        if ident in seen:
            continue
        if c["relation"] == "yoy" and (c["metric_key"], _text_period_kind(c["source_sentence"]),
                                       c["caliber"]) in levels:
            continue
        seen.add(ident)
        out.append(c)
    _dedupe(out)
    return out


#: 断言身份 = 指标 + 期间 + 口径 + 关系 + 值（+ 事实/预测）
_IDENT_KEYS = ("metric", "period_kind", "caliber", "relation", "value", "unit", "is_forecast")


def _claim_identity(c: dict) -> tuple:
    """一条主张的身份：指标 + 期间 + 口径 + 关系 + 值。

    去重必须按身份来，不能只看「指标 + 值」—— 同一家公司两年同值（2023 年 42.02 亿、
    2022 年 42.02 亿）是两条事实断言，按值去重会凭空吃掉一条。
    """
    text = str(c.get("claim_text") or "")
    metric = str(c.get("metric") or "")
    key = c.get("metric_key") or resolve_metric(metric) or resolve_metric(text)
    unit = str(c.get("unit") or "")
    period = str(c.get("period_kind") or "") or str(c.get("period") or "") or _text_period_kind(text)
    caliber = str(c.get("caliber") or "") or _claim_caliber(text)
    relation = str(c.get("relation") or "") or _claim_relation(key, unit)
    fc = c.get("is_forecast")
    fc = bool(fc) if fc is not None else bool(_FORECAST_BOUNDARY_RX.search(text))
    return (metric, period, caliber, relation, str(c.get("value", "")), unit, fc)


def _dedupe(claims: list[dict]) -> None:
    seen: set[tuple] = set()
    keep = []
    for c in claims:
        k = _claim_identity(c)
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
        assertion=str(d.get("assertion", "") or "").strip(),
        metric_key=key,
        value=value,
        unit=str(d.get("unit", "") or "").strip(),
        period=str(d.get("period", "") or ""),
        source_sentence_id=str(d.get("source_sentence_id", "") or ""),
        claim_group_id=str(d.get("claim_group_id", "") or ""),
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


def _fmt_evidence_amount(value: float, unit: str, base_unit: str) -> str:
    """证据里的金额怎么写：先给材料自己的单位，不是基准单位时补一句换算。

    否则读者会把「415,377,697 千元」读成 415,377,697 元，照着「修改建议」改就错一千倍。
    材料没标单位、而基准单位是比率（%/倍/百分点）时不许硬补一个百分号 ——
    那会把「营业收入 4,202,030,347.42 元」写成「4,202,030,347.42%」。
    """
    if not unit and base_unit in ("%", "倍", "个百分点"):
        return f"{_fmt(value)}（材料未标单位）"
    u = unit or base_unit
    shown = f"{_fmt(value)}{u}"
    if unit and unit != base_unit and base_unit == "元" and unit in MONEY_SCALE:
        conv = value * MONEY_SCALE[unit] / 1e8
        if conv >= 0.01:
            shown += f"（合 {conv:,.2f}亿元）"
    return shown


def _ev(page: PageRec, tok: Token | None, quote: str) -> dict:
    """一条证据：原始值 + 出处页 + 行标签/列（能从表格绑定读到时一并带上）。

    原始值与行列要留在证据里：报告与红框都靠它定位，事后核对也才有据可查
    （只留一个「第 N 页」没法回答「你说的到底是哪一格」）。
    """
    return {
        "source_id": page.source_id,
        "file_name": page.file_name,
        "source_type": page.source_type,
        "publication_date": page.publication_date,
        "page": page.page,
        "value": tok.value if tok else None,
        "unit": tok.unit if tok else "",
        "raw": tok.raw if tok else "",
        "row_label": (tok.before[-40:] if tok and tok.before else ""),
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
    # 算术只走 calculators/consistency.py 的 Decimal 实现：人工摘录那条路径调的是同一个
    # 函数，本模块不再自己写一套浮点公式 —— 两套公式迟早算出两个数。
    expected = expected_dividend_yuan(shares, per10)
    how = (f"{shares:,.0f} 股 ÷ 10 × {per10} 元 = {expected} 元"
           f"（公式：股本 × 每10股派息数 ÷ 10；舍入：四舍五入到分）")
    return float(expected), ev, how


def _is_primary_statement(page: "PageRec") -> bool:
    """该页是否为「主要会计数据和财务指标」摘要表。

    材料内部同一指标出现互不相容的两处披露时，以这张表为准——它是年报的法定摘要
    口径，其余章节（经营情况讨论、附注）都可能抄错。
    """
    return "主要会计数据和财务指标" in page.flat


# ---------------------------------------------------------------------------
# 摘要表：把候选值绑到「哪一行、哪一年那一列」
# ---------------------------------------------------------------------------
#
# 只按「哪一页、哪个指标」取候选会造出假证据：2021 年那一列营业收入（45.12 亿）
# 会被拿去支持「预计 2025 年营收 45.2 亿」。这里直接读 PDF 的表格网格（pymupdf
# find_tables），把行标签与列头（四位年份 / 本期比上年同期增减(%)）绑起来，
# 只允许同年份那一列给同年份的主张作证据。网格读不出来时退回邻域取候选。

RATIO_CELL = "增减(%)"                     # 「本期比上年同期增减(%)」列
_YEAR_CELL = re.compile(r"^(20\d{2})\s*年(?:末|度)?$")
_YEAR_RX = re.compile(r"(20\d{2})")
_YRANGE_RX = re.compile(r"(20\d{2})\s*[Ee]?\s*年?\s*[-–—~/至]\s*(20\d{2})\s*[Ee]?(?!\d)")
_YM_RX = re.compile(r"(20\d{2})\s*[-/年]\s*(\d{1,2})\s*月?")
_YMD_RX = re.compile(r"(20\d{2})\s*[-/年]\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})")
#: 半年/季度写法，两种排布都要认：「2026H1」年份在前，「1H26」年份在后（两位年按 20xx 补）。
#: 这里一律用 (?<!\d)...(?!\d) 划边界而不是 \b —— \w 在 Unicode 下包含汉字，
#: 「截至1H26」这种紧贴汉字的写法用 \b 会匹配不上。
_HH_YEAR_FIRST = re.compile(r"(20\d{2})\s*年?\s*[Hh]\s*([12])(?!\d)")
_HH_YEAR_LAST = re.compile(r"(?<!\d)([12])\s*[Hh]\s*(\d{2,4})(?!\d)")
_HQ_YEAR_FIRST = re.compile(r"(20\d{2})\s*年?\s*[Qq]\s*([1-4])(?!\d)")
_HQ_YEAR_LAST = re.compile(r"(?<!\d)([1-4])\s*[Qq]\s*(\d{2,4})(?!\d)")
_HALF_CN = re.compile(r"(20\d{2})\s*年\s*(上|下)半年")
_CELL_NUM = re.compile(r"^-?\(?\d[\d,]*(?:\.\d+)?\)?%?$")
_RATIO_HDR = re.compile(r"增减|比上|同期|\(%\)|（%）|^%$")
_NO_NUM = ("不适用", "无", "-", "—", "／", "/")

#: 材料页面自报的金额单位（「单位：千元  币种：人民币」这类）。
#: 摘要表上的数字必须按它换算：工业富联 2018 年报第 10 页的 415,377,697 是千元，
#: 当成元用会把「修改建议」写成 415,377,697 元（比真值小一千倍），比不报还危险。
_PAGE_UNIT_RX = re.compile(r"单位\s*[:：]\s*(百万元|千元|万元|元)")


def page_declared_unit(page: "PageRec") -> str:
    """页面自报的金额单位；读不到返回空串（空串表示未知，不做任何换算）。"""
    m = _PAGE_UNIT_RX.search(page.flat)
    return m.group(1) if m else ""


def _table_unit(rows: list[list[str]]) -> str:
    """表格自己写出来的金额单位（表内常有「单位：千元」一行，比页面级更贴近该表）。"""
    for row in rows:
        for cell in row:
            m = _PAGE_UNIT_RX.search(cell or "")
            if m:
                return m.group(1)
    return ""


@dataclass(frozen=True)
class BoundCell:
    """摘要表里一个数值：绑定了它所在的行标签、列，以及表自报的单位。"""

    label: str
    year: str          # 四位年份；RATIO_CELL 表示增减率列；"" 表示列头认不出来
    value: float
    raw: str
    unit: str = ""     # "" = 该页没写单位


def _cell_text(cell) -> str:
    t = re.sub(r"\s+", "", cell or "")
    return t.translate(str.maketrans("（）％－", "()%-"))


def _cell_num(text: str) -> float | None:
    if text in _NO_NUM or not _CELL_NUM.match(text):
        return None
    s = text.strip("()%").replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def _table_headers(rows: list[list[str]]) -> list[tuple[int, dict[int, str]]]:
    """表头行 → {列号: 年份 / RATIO_CELL}。折行的表头（本年比上年增 / 减）合并成一列。"""
    heads: list[tuple[int, dict[int, str]]] = []
    for i, row in enumerate(rows):
        yc = {j: _YEAR_CELL.match(c).group(1) for j, c in enumerate(row) if _YEAR_CELL.match(c)}
        if len(yc) >= 2:
            m = dict(yc)
            for j, c in enumerate(row):
                if j not in m and _RATIO_HDR.search(c):
                    m[j] = RATIO_CELL
            heads.append((i, m))
        elif heads and not any(_cell_num(c) is not None for c in row):
            last = heads[-1][1]
            for j, c in enumerate(row):
                if c in ("增", "减", "%", "%)", "（%）") or _RATIO_HDR.search(c):
                    last.setdefault(j, RATIO_CELL)
    return heads


def bound_cells(page: "PageRec", aliases: Iterable[str]) -> list[BoundCell]:
    """摘要表里命中该指标的单元格（带行标签、列年份与表自报的单位）。"""
    al = [flat(a) for a in aliases]
    out: list[BoundCell] = []
    for tab in page.tables:
        rows = [[_cell_text(c) for c in row] for row in tab]
        # 「单位：千元」写在表里就以表为准，没写才回落到整页声明；都没有就是空串。
        unit = _table_unit(rows) or page_declared_unit(page)
        heads = _table_headers(rows)
        pending: list[str] = []
        i = 0
        while i < len(rows):
            row = rows[i]
            if sum(1 for c in row if _YEAR_CELL.match(c)) >= 2 or not any(row):
                i += 1
                continue
            nums = [(j, _cell_num(c), c) for j, c in enumerate(row)]
            nums = [(j, v, c) for j, v, c in nums if v is not None]
            if not nums:
                txt = "".join(c for c in row if c and not _YEAR_CELL.match(c))
                if txt:
                    pending.append(txt)
                i += 1
                continue
            label = "".join(pending) + "".join(
                c for j, c in enumerate(row) if c and _cell_num(c) is None)
            pending = []
            row_idx = i
            # 行标签在表里竖着折行（「归属于上市公 / 司股东的净资 / 产（元）」）
            j2 = i + 1
            while (j2 < len(rows) and "".join(rows[j2])
                   and not any(_cell_num(c) is not None for c in rows[j2])
                   and sum(1 for c in rows[j2] if _YEAR_CELL.match(c)) < 2):
                label += "".join(rows[j2])
                j2 += 1
            i = j2
            if not any(a in flat(label) for a in al):
                continue
            cmap: dict[int, str] = {}
            for hi, hm in heads:
                if hi < row_idx:
                    cmap = hm
                else:
                    break
            for j, v, raw in nums:
                if j in cmap:
                    year = cmap[j]
                else:
                    prev = [jj for jj in cmap if jj < j]
                    year = cmap[max(prev)] if prev else ""
                if not year and (raw.endswith("%") or "个百分点" in raw):
                    year = RATIO_CELL          # 列头认不出来时，按数值形态挡掉增减列
                out.append(BoundCell(label=label, year=year, value=v, raw=raw, unit=unit))
    return out


def _col_header_matches(cell: str, alias: str) -> bool:
    """表头单元格是不是这个指标的列：允许「毛利率」「毛利率(%)」这种括号装饰。

    必须排除「毛利率比上年增减」这类相邻列 —— 那是变动列，不是水平值。
    """
    c, a = flat(cell), flat(alias)
    if not c or not a or not c.startswith(a):
        return False
    rest = c[len(a):]
    return all(ch in "()%－-—~·:：（）[]【】／/ " for ch in rest)


_SUBJECT_STOP = ("公司", "本", "期", "年", "年度", "报告期", "为", "的", "是", "较", "上年",
                 "同期", "同比", "减少", "增加", "下降", "上升", "增长", "降低", "个百分点",
                 "亿元", "万元", "千元", "元", "约", "实现", "达到", "已", "在", "其中")
_SECTION_RX = re.compile(r"(主营业务)?分(行业|产品|地区|销售模式)情况|收入和成本分析")


def _is_section_title(line: str) -> bool:
    """这一行是不是「主营业务分行业情况」这类小节标题。"""
    return bool(_SECTION_RX.search(flat(line or "")))


def _subject_tokens(text: str, aliases: Iterable[str] = ()) -> list[str]:
    """主张里可能对应表行标签的中文片段（「红人营销行业」这种）。

    先把指标别名和常见虚词去掉，否则「公司2023年红人营销行业毛利率为21.75%」会被
    整段当成一个片段，和表里的行标签「红人营销行业」对不上。
    """
    t = text or ""
    for a in aliases:
        t = t.replace(a, " ")
    for w in _SUBJECT_STOP:
        t = t.replace(w, " ")
    return [s for s in re.findall(r"[\u4e00-\u9fa5]{2,}", t)]


def _value_line_num(line: str) -> float | None:
    """一格一行的原始文本行是不是数值：纯数字，或「减少3.73 个百分点」这种带方向的值。"""
    v = _cell_num(line)
    if v is not None:
        return v
    m = re.fullmatch(r"\s*(减少|下降|降低|增加|上升|增长|持平)?\s*(-?[\d,]+(?:\.\d+)?)\s*"
                     r"(个百分点|%|％)?\s*", line or "")
    if not m:
        return None
    num = _to_float(m.group(2).replace(",", ""))
    if num is None:
        return None
    return -num if m.group(1) in ("减少", "下降", "降低") else num


def _merge_label_units(head: list[str]) -> list[str]:
    """把折行的表头合成一个个列名：PDF 会把「营业收入比上 / 年增减（%）」拆成两行。"""
    units: list[str] = []
    for ln in head:
        text = flat(ln)
        if not text:
            continue
        if not units:
            units.append(text)
            continue
        prev = units[-1]
        complete = prev.endswith("）") or prev.endswith(")") or len(prev) <= 4
        if complete:
            units.append(text)
        else:
            units[-1] = prev + text
    return units


def bound_text_columns(page: "PageRec", aliases: Iterable[str], *,
                       subject: str = "", metric_unit: str = "") -> list[BoundCell]:
    """一页被拆成「一格一行」时，按原始行做列绑定（tables 解析不出网格的分行业表）。

    行 = 行业、列 = 指标：表头是若干标签行，数据行是「行标签 + 若干数值行」。
    列数对不上、或没有可信的行标签时返回空（宁可降级，也不拿相邻列的数值充数）。
    """
    lines = [ln.strip() for ln in (page.raw or "").splitlines()]
    al = [flat(a) for a in aliases]
    subs = _subject_tokens(subject, aliases)
    if not subs:
        return []
    out: list[BoundCell] = []
    for li, ln in enumerate(lines):
        if not ln or _value_line_num(ln) is not None:
            continue
        if not any(s in flat(ln) for s in subs):
            continue
        values: list[tuple[str, float]] = []
        lj = li + 1
        while lj < len(lines) and lines[lj]:
            v = _value_line_num(lines[lj])
            if v is None:
                break
            values.append((lines[lj], v))
            lj += 1
        if not values:
            continue
        head: list[str] = []
        lk = li - 1
        while lk >= 0 and lines[lk] and _value_line_num(lines[lk]) is None \
                and not _is_section_title(lines[lk]):
            head.insert(0, lines[lk])
            lk -= 1
        if not head:
            continue
        units = _merge_label_units(head)
        if len(units) == len(values) + 1:
            units = units[1:]                     # 表头第一列是行标签列，不占数值位
        if len(units) != len(values):
            continue                              # 列数对不上，不敢绑
        for idx, unit in enumerate(units):
            if not any(_col_header_matches(unit, a) for a in al):
                continue
            raw, val = values[idx]
            if metric_unit in ("%", "个百分点", "倍") and not -1000 <= val <= 1000:
                continue                          # 比率列不允许出现巨额数字（那是别的列）
            out.append(BoundCell(label=flat(ln), year="", value=val, raw=raw,
                                 unit=metric_unit or (page_declared_unit(page) or "")))
    return out


def bound_column_cells(page: "PageRec", aliases: Iterable[str], *,
                       subject: str = "", metric_unit: str = "") -> list[BoundCell]:
    """表里「指标在列头」时的取值：按指标列绑定，而不是按行。

    摘要表是「行=指标、列=期间」，主营业务分行业情况表却相反：行是行业、列是指标 ——
    毛利率是列头，红人营销行业才是行。只做行绑定会绑不上，于是退回邻域取数，把同一行
    相邻的营业收入当成毛利率的披露值，报告就给出「毛利率应改为 4,202,030,347.42%」。
    行绑定失败时用这个补：拿指标列 + 主张里出现的那一行（行业），取交叉单元格。
    行标签无法可靠确定（匹配不上或匹配到多行）时返回空，交给上层降级。
    """
    al = [flat(a) for a in aliases]
    subs = _subject_tokens(subject, aliases)
    out: list[BoundCell] = []
    best: dict[int, int] = {}          # 行号 → 命中的最长片段长度
    hits: list[tuple[int, int, float, str, str]] = []
    for tab in page.tables:
        rows = [[_cell_text(c) for c in row] for row in tab]
        unit = _table_unit(rows) or page_declared_unit(page)
        cell_unit = metric_unit if metric_unit in ("%", "个百分点", "倍") else unit
        for hi, hrow in enumerate(rows):
            for j, cell in enumerate(hrow):
                if not any(_col_header_matches(cell, a) for a in al):
                    continue
                for ri in range(hi + 1, len(rows)):
                    row = rows[ri]
                    if sum(1 for c in row if _YEAR_CELL.match(c)) >= 2:
                        break                      # 下一张表的表头，停止
                    if len(row) <= j:
                        continue
                    v = _cell_num(row[j])
                    if v is None:
                        continue
                    label = "".join(c for k, c in enumerate(row)
                                    if k != j and c and _cell_num(c) is None)
                    label = label or "".join(row)
                    matched = max((len(s) for s in subs if s in flat(label)), default=0)
                    hits.append((ri, matched, v, row[j], label))
                    if matched:
                        best[ri] = max(best.get(ri, 0), matched)
    if not hits:
        return []
    if subs:
        top = max(best.values(), default=0)
        if top < 2:
            return []                              # 行标签对不上 → 不敢绑
        winners = [ri for ri, ln in best.items() if ln == top]
        if len(winners) != 1:
            return []                              # 好几行都像 → 不敢绑
        keep = winners[0]
        hits = [h for h in hits if h[0] == keep]
    for _ri, _m, v, raw, label in hits:
        out.append(BoundCell(label=label, year="", value=v, raw=raw, unit=cell_unit))
    return out


def _norm_year(raw: str) -> int:
    """两位年份补全成 20xx：「1H26」→ 2026。"""
    y = int(raw)
    return y if y >= 1000 else 2000 + y


def _month_end(year: int, month: int) -> date | None:
    return date(year, month, calendar.monthrange(year, month)[1]) if 1 <= month <= 12 else None


def _hq_period_end(p: str) -> date | None:
    """半年度/季度写法的期间末日。

    这类写法没有 20xx 开头的四位年，落到只认四位年的兜底逻辑上会返回 None，
    而 `_out_of_scope` 遇到 None 就静默放行 —— 于是「1H26 营收 5578.61 亿元」
    会被拿去和截止日之前的年报数据比，判成「矛盾」。必须在这里认出来。
    """
    m = _HH_YEAR_FIRST.search(p)
    if m:
        return _month_end(int(m.group(1)), 6 if m.group(2) == "1" else 12)
    m = _HH_YEAR_LAST.search(p)
    if m:
        return _month_end(_norm_year(m.group(2)), 6 if m.group(1) == "1" else 12)
    m = _HQ_YEAR_FIRST.search(p)
    if m:
        return _month_end(int(m.group(1)), int(m.group(2)) * 3)
    m = _HQ_YEAR_LAST.search(p)
    if m:
        return _month_end(_norm_year(m.group(2)), int(m.group(1)) * 3)
    m = _HALF_CN.search(p)
    if m:
        return _month_end(int(m.group(1)), 6 if m.group(2) == "上" else 12)
    return None


def _period_year(period: str) -> int | None:
    d = _period_end(period)
    return d.year if d else None


#: 定期报告的期间口径。列头只写年份，这一列到底是整年数还是半年数，得看它出自哪种报告。
_HALF_CN_FILE_RX = re.compile(r"半年度报告|半年报|中期报告")
_QUARTER_FILE_RX = re.compile(r"第\s*([一二三四])\s*季度报告")
_QUARTER_KIND = {"一": "q1", "二": "h1", "三": "q3", "四": "annual"}


def _page_period_kind(page: "PageRec") -> str:
    """这一页所属报告的期间口径：annual / h1 / q1 / q3（认不出来按整年，保持老口径）。"""
    name = page.file_name or ""
    if (page.source_type or "") == "original_interim_report" or _HALF_CN_FILE_RX.search(name):
        return "h1"
    m = _QUARTER_FILE_RX.search(name)
    if m:
        return _QUARTER_KIND[m.group(1)]
    return "annual"


def _column_period_end(page: "PageRec", year: int) -> date:
    """摘要表某一列代表的期间末日（列头只说年份，口径按所属报告定）。"""
    kind = _page_period_kind(page)
    if kind == "h1":
        return date(year, 6, 30)
    if kind == "q1":
        return date(year, 3, 31)
    if kind == "q3":
        return date(year, 9, 30)
    return date(year, 12, 31)


def _column_matches_claim(page: "PageRec", col_year: str, claim_per: date | None) -> bool:
    """摘要表这一列能不能给这条主张当证据 —— 两边的期间末日要对得上。

    只比「年份」不够：年报里「2026 年」那一列是整年数，半年报里同一个列头是上半年数。
    主张说「2026 年上半年」却去和年报的整年数比，数值凑上了就判「支持」，
    等于替一个口径错的主张背书；反过来，列头年份对得上但口径不同（半年 vs 整年）也一样。
    """
    if claim_per is None:
        return True                     # 主张期间认不出来：不在这里拦，交给资格门降级
    if not (col_year or "").isdigit():
        return True                     # 列头认不出来就没法比，保持老口径
    return _column_period_end(page, int(col_year)) == claim_per


def _claim_period_year(claim: "Claim") -> int | None:
    """主张所指的年份：期间字段优先，字段认不出来再看正文自己写没写。"""
    per = _claim_period(claim)
    return per.year if per else None


def _material_default_period(idx: "MaterialIndex") -> date | None:
    """材料自己的报告期 = 最新那份定期报告的期间末日。

    主张只说「本期」「同比」而不写年份时，它指的就是这个期间 —— 规则抽取常常不填
    期间字段，正文也常只写「本期」，这不是「期间不明」，而是「期间由材料确定」。
    """
    best: date | None = None
    for p in idx.pages:
        if p.source_type not in _PERIODIC_TYPES:
            continue
        m = _REPORT_YEAR_RX.search(p.file_name or "")
        if not m:
            continue
        end = _column_period_end(p, int(m.group(1)))
        if best is None or end > best:
            best = end
    return best


def _effective_claim_period(claim: "Claim", idx: "MaterialIndex") -> date | None:
    """主张的所指期间：自己写明的优先，没写就按材料的报告期算；都不行才是「说不清」。"""
    return _claim_period(claim) or _material_default_period(idx)


def _period_end(period: str) -> date | None:
    """主张所指期间的末日：2025 → 2025-12-31；2025-06 → 2025-06-30；1H26 → 2026-06-30。"""
    p = (period or "").strip()
    if not p:
        return None
    d = _YMD_RX.search(p)
    if d:
        try:
            return date(int(d.group(1)), int(d.group(2)), int(d.group(3)))
        except ValueError:
            return None
    # 年份区间：2025-2027年 / 2024年-2026年 / 2025~2027 —— 期间末日取最后那一年年末。
    # 必须先于年月判断：否则 "2025-2027" 会被 _YM_RX 当成「2025 年 20 月」，整条主张就丢了预测类。
    rng = _YRANGE_RX.search(p)
    if rng:
        return date(int(rng.group(2)), 12, 31)
    hq = _hq_period_end(p)
    if hq:
        return hq
    m = _YM_RX.search(p)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12:
            return date(y, mo, calendar.monthrange(y, mo)[1])
        # 月份离谱（被区间骗到之类）→ 不返回 None，落到下面按年份兜底
    y = _YEAR_RX.search(p)
    return date(int(y.group(1)), 12, 31) if y else None


def _out_of_scope(claim: "Claim", cutoff: str | None) -> bool:
    """主张所指期间晚于预警截止日 → 材料里不可能有它的披露值。

    期间字段认不出来时再看正文自己写没写（规则抽取不填 period，「1H26」却在正文里）。
    """
    if not cutoff:
        return False
    end = _claim_period(claim)
    if end is None:
        return False
    try:
        cut = parse_date(cutoff)
    except (ValueError, TypeError):
        return False
    return end > cut


def _coverage_check(claims: list["Claim"], cutoff: str | None) -> str | None:
    """材料窗口与草稿期间到底有没有交集；没有交集就返回一句要说给用户听的话。

    截止日是按「材料披露日里最早的那份」推出来的（事前预警模式），一旦草稿换成真实研报、
    期间比材料新，材料集就整体落在窗口之外 —— 此时逐条结论没有对照面，必须响亮地说出来，
    而不是照常交出一份看起来很完整的报告（历史事故：2026 年研报配 2018 年报，1H26/2Q26
    的主张被拿去和 2018 年的数据比，8 条判成「矛盾」）。
    """
    if not cutoff:
        return None
    try:
        cut = parse_date(cutoff)
    except (ValueError, TypeError):
        return None
    ends = [(_period_end(c.period), c) for c in claims if c.metric_key and c.value is not None]
    known = [(e, c) for e, c in ends if e is not None]
    # 有期间认不出来的主张，或者有一条落在窗口里，都不报这条：交集存在，逐条判定就是有意义的。
    if not known or len(known) != len(ends):
        return None
    beyond = [(e, c) for e, c in known if e > cut]
    if len(beyond) != len(known):
        return None
    latest = max(e for e, _ in known)
    return (f"本次材料覆盖不到草稿期间：草稿里 {len(known)} 条可核验主张全部晚于预警截止日 "
            f"{cutoff}（最晚期间末日 {latest.isoformat()}），材料中不可能有它们的对照披露。"
            f"逐条结论不构成对草稿事实性的判断；请补入覆盖草稿期间的定期报告"
            f"（如对应年度的半年报 / 年报）后重跑。")


#: 预测/观点用语：研报里这类句子不是披露事实，优先级高于任何数值比对
#: （历史事故：「预计2026-2028年营业收入…」被拿去和 2018 年报比，判成「矛盾」）。
_FORECAST_RX = re.compile(
    r"预计|预测|料将|有望|目标价|合理价值|盈利预测|我们预计|上调|下调|买入|增持|减持|评级"
    r"|(?<!\d)20\d{2}\s*[Ee](?![A-Za-z])"
)

#: 主张文本里明确写出来的期间：规则抽取不写 period 字段，正文却常自带「1H26」「2026-2028年」。
#: 只认显式期间写法，不认裸四位数年份 —— 否则 120234567 里的 2023 也会被当成期间。
_TEXT_PERIOD_RX = re.compile(r"(?<!\d)20\d{2}\s*(?:年|[Ee](?![A-Za-z])|[HhQq][1-4])"
                            r"|(?<!\d)[12]\s*[HhQq]\s*\d{2,4}(?!\d)")

#: 定期报告的期间年份：从文件名取（「2019-03-29_…_2018年年度报告.PDF」→ 2018）。
_REPORT_YEAR_RX = re.compile(
    r"(?<!\d)(20\d{2})\s*年\s*(?:年度报告|半年度报告|第[一二三四]季度报告|年报|半年报|季报|年度|半年度)")
_PERIODIC_TYPES = ("original_annual_report", "original_interim_report")


def _fmt_years(years: Iterable[int]) -> str:
    ys = sorted(years)
    if len(ys) > 3:
        return f"{ys[0]}–{ys[-1]} 年"
    return "、".join(str(y) for y in ys) + " 年"


def _period_from_text(text: str) -> "date | None":
    """从主张文本里读期间：先定位显式期间写法，再把那一小段交给 _period_end 取末日。"""
    if not text:
        return None
    m = _TEXT_PERIOD_RX.search(text)
    if not m:
        return None
    return _period_end(text[m.start():m.start() + 24])


def _claim_period(claim: "Claim") -> "date | None":
    """主张所指期间的末日：字段优先，字段认不出来再看正文自己写没写。

    正文先看**子句**（断言自己的边界），子句没写期间且这条不是预测时，再看整句 ——
    「2023 年，公司实现营业收入 42.02 亿元」这种把年份放在句首的写法不该算期间不明。
    """
    per = _period_end(claim.period) or _period_from_text(claim.claim_text)
    if per is None and not _is_forecast_text(claim):
        per = _period_from_text(_context_text(claim))
    return per


def _context_text(claim: "Claim") -> str:
    """判指标、口径、方向时用的上下文：整句。

    子句是**断言**的边界，但口径限定语常在主句之前：「按归母净利润口径，…ROE 为 1.66%」
    只取子句会丢掉「归母」，把口径错的主张判成「支持」（实测踩过）。
    """
    return str((claim.raw or {}).get("source_sentence") or claim.claim_text or "")


def _material_report_years(pages: Iterable["PageRec"]) -> set[int]:
    """纳入材料里的定期报告覆盖哪些年份 —— 用来判断草稿到底是不是在讲这些材料。

    除了报告年份本身，还算上表里出现的比较期间列：年报里的「2022 年」列就是对 2022 年的
    披露，主张说 2022 年时不能因为「这份报告是 2023 年报」就判它期间不明。
    """
    years: set[int] = set()
    for p in pages:
        if p.source_type not in _PERIODIC_TYPES:
            continue
        m = _REPORT_YEAR_RX.search(p.file_name or "")
        if m:
            years.add(int(m.group(1)))
        for table in (p.tables or ()):
            for row in table:
                for cell in row:
                    cm = _YEAR_CELL.match(str(cell).strip())
                    if cm:
                        years.add(int(cm.group(1)))
    return years


def _draft_years(text: str) -> set[int]:
    """草稿里出现的年份。1990–2060 之外的多半是 OCR 噪声（研报表里夹着的错字数字）。"""
    return {int(y) for y in re.findall(r"(?<!\d)20\d{2}(?!\d)", text or "")
            if 1990 <= int(y) <= 2060}


def _case_overlap_gate(pages: list["PageRec"], draft_text: str) -> str | None:
    """材料讲的年份和草稿讲的年份有没有交集；没有就返回一句要说给用户听的话。

    这是证据资格门的第一道，案件级的：材料整份落在草稿讨论的期间之外时，逐条数值比对
    没有意义（历史事故：2026 年半年报点评配 2018 年年报，15 条主张全被判「矛盾」）。
    """
    mat = _material_report_years(pages)
    want = _draft_years(draft_text)
    if not mat or not want or (mat & want):
        return None
    return (f"本次材料与草稿没有重叠期间：草稿通篇讨论 {_fmt_years(want)}，而纳入的材料是 "
            f"{_fmt_years(mat)}的定期报告，草稿里没有提到这个年度。拿不覆盖草稿期间的材料比数，"
            f"只会得到假矛盾 —— 本次已停用「矛盾」判定，逐条按证据不足处理；"
            f"请补入草稿所涉期间的定期报告后重跑。")


def _is_forecast_text(claim: "Claim") -> str | None:
    """主张是否带预测/观点用语；只看**主张数值所在的那个子句**。

    一句里「本年实际收入…预计次年收入…」的事实子句不能被旁边的预测子句连坐
    （历史问题：实际值也被归为「预测」）。规则抽取在拆句时已知子句，优先用它记下的标志；
    模型拆出的长句则在主张自己那个数值所在的子句里找。
    """
    flag = (claim.raw or {}).get("is_forecast")
    text = claim.claim_text or ""
    if isinstance(flag, bool):
        return (_FORECAST_RX.search(text) or _FORECAST_BOUNDARY_RX.search(text)).group(0) if flag else None
    m = _FORECAST_RX.search(text)
    if not m:
        return None
    pos = _value_pos_in(text, claim)
    if pos is None:
        return m.group(0)          # 数值不在文本里（模型改写过的断言），只能按整句判
    lo, hi = _clause_span(text, pos)
    hit = _FORECAST_RX.search(text[lo:hi])
    return hit.group(0) if hit else None


#: 可核验主张少于这个数，就不拿「全部矛盾」当异常信号（样本太小）。
_ANOMALY_MIN_CLAIMS = 3


def _all_contradicted_anomaly(rows: list[dict]) -> str | None:
    """可核验主张 100% 判「矛盾」本身就是系统异常信号，不是研报的结论。

    真研报极少整篇全错；出现这种比例，先怀疑材料期间/口径对不上或抽取质量不达标，
    而不是照着 15 条「矛盾」去改研报。
    """
    verifiable = [r for r in rows if r["verdict"]["label"] in
                  (SUPPORTED, CONTRADICTED, INSUFFICIENT)]
    contra = [r for r in verifiable if r["verdict"]["label"] == CONTRADICTED]
    if len(verifiable) < _ANOMALY_MIN_CLAIMS or len(contra) != len(verifiable):
        return None
    return (f"系统异常信号：本次 {len(verifiable)} 条可核验主张全部判为「矛盾」（100%）。"
            f"真实研报极少整篇全错，这个比例更可能说明材料期间/口径与草稿对不上、"
            f"或抽取质量不达标 —— 请先怀疑本次配置，不要据此逐条改动研报。")


def _contradiction_block(claim: "Claim", idx: "MaterialIndex", *,
                         allow_contradiction: bool = True,
                         block_reason: str | None = None) -> str | None:
    """「矛盾」是全项目最重的标签，必须过证据资格门；不满足就降级为「证据不足」。

    宁可说「暂时无法验证」，也不能让系统轻易说出「券商写错了」。
    """
    if not allow_contradiction:
        return block_reason or "本次材料与草稿没有重叠期间，不判矛盾。"
    # ① 主张必须自己点名指标：拆句残留（如只剩「15.2%」）撑不起一条事实断言
    ctx_text = _context_text(claim)
    if resolve_metric(ctx_text) is None:
        return (f"主张文本「{ctx_text[:40]}」里没有出现指标名，无法确认它说的是"
                f"哪个指标（疑为拆句残留），不判矛盾。")
    # ② 期间必须可比：主张讲的期间要落在材料覆盖的年份里
    cp = _claim_period(claim)
    mat = _material_report_years(idx.pages)
    if cp is None and _effective_claim_period(claim, idx) is None:
        return ("期间不明：主张既没写期间，材料里也没有定期报告可以给出默认报告期，"
                "无法确定要拿哪一期披露与它对照，不判矛盾。")
    if cp is not None and mat and cp.year not in mat:
        return (f"主张所指期间是 {cp.year} 年，本次材料只覆盖 {_fmt_years(mat)}，"
                f"两者期间不匹配：材料里不可能有这一期间的对照披露，不判矛盾"
                f"（最多只能说明材料内没有可比证据）。")
    return None


def _support_block(claim: "Claim", idx: "MaterialIndex") -> str | None:
    """「支持」和「矛盾」用同一道证据资格门。

    判「支持」是在替草稿的事实性背书，门槛不能比判「矛盾」低：主张得自己点名指标、
    说清是那一期，而且那一期要在材料覆盖的期间里。少了这条，「本年主张命中上一年数值」
    就会被判成支持（期间字段认不出来时列绑定放行，数值又凑巧一样）。
    """
    ctx_text = _context_text(claim)
    if resolve_metric(ctx_text) is None:
        return (f"主张文本「{ctx_text[:40]}」里没有出现指标名，无法确认它说的是"
                f"哪个指标（疑为拆句残留），不足以判「支持」。")
    per = _effective_claim_period(claim, idx)
    if per is None:
        return ("主张没有写明确切的所指期间，材料里也认不出自己的报告期，"
                "无法确认它指的是哪一期、材料里那处数值是不是同一期，不足以判「支持」。")
    mat = _material_report_years(idx.pages)
    if mat and per.year not in mat:
        return (f"主张所指期间是 {per.year} 年，本次材料只覆盖 {_fmt_years(mat)}，"
                f"两者期间不匹配：不足以判「支持」（材料里没有这一期间的对照披露）。")
    return None


def _period_mismatch_note(claim: "Claim", idx: "MaterialIndex") -> str | None:
    """主张期间和材料覆盖的期间对不上时的说明；说得出才返回。

    「支持」和「矛盾」共用这一句：同一道门挡住的东西，得给同一个理由。
    """
    per = _claim_period(claim)
    mat = _material_report_years(idx.pages)
    if per is None or not mat or per.year in mat:
        return None
    return (f"主张所指期间是 {per.year} 年（{per.isoformat()}），本次材料只覆盖 {_fmt_years(mat)}，"
            f"两者期间不匹配：材料里不可能有这一期间的对照披露"
            f"（最多只能说明材料内没有可比证据）。")


def _prediction(claim: "Claim", cutoff: str) -> Verdict:
    """期间晚于预警截止日（或认不出期间）时的专门结论。

    这里要写清**是哪一种**：带预测用语、期间落在材料窗口外、还是期间根本认不出来 ——
    三种混成一句「属预测/观点」会掩盖真实原因，用户没法据此判断该补材料还是改写。
    """
    per = _claim_period(claim)
    what = (f"主张所指期间（{claim.period}）" if claim.period
            else f"主张正文写明的期间（{per.isoformat()}）" if per else "该主张所指期间")
    return Verdict(
        label=PREDICTION, confidence=1.0,
        basis=(f"{what}晚于预警截止日 {cutoff}，材料中不可能存在该期间的"
               f"披露值：属预测/观点，不是可核验的披露事实，单列一类、不计入核查分母。"
               f"（本次是按**期间**归类，主张正文里没有预测用语；若该期数据确已披露，"
               f"说明材料集缺了对应报告。）"),
        unable_reason="期间晚于预警截止日（材料窗口外），按预测·观点单列。",
    )


def _unknown_period() -> Verdict:
    """期间认不出来时的专门结论：不猜、也不塞进「预测」那一类。"""
    return Verdict(
        label=INSUFFICIENT, confidence=0.3,
        basis=("主张没写明确切的所指期间，材料里也认不出自己的报告期，无法确定该拿哪一期"
               "披露与它对照 —— 既不能判「支持」，也不按预测·观点处理（那是另一类）。"),
        unable_reason="期间不明：主张与材料都没给出可用的报告期。",
        missing_input="该主张对应的报告期（如 2023A / 2026H1）或缺该期的定期报告。",
    )


def _ratio_verdict(claim: "Claim", spec: dict, idx: "MaterialIndex") -> Verdict | None:
    """同比/增减率主张：摘要表「本期比上年同期增减(%)」列就是它的披露值。

    单位是 % 的主张不能按绝对额换算（12.43 亿元的同比 -19.33% 与「元」不同维度），
    但年报摘要表对这一行本来就给出了增减率，直接比那一列即可。
    """
    got: list[tuple[PageRec, BoundCell]] = []
    for page in idx.alias_pages(spec["aliases"]):
        for bc in bound_cells(page, spec["aliases"]):
            if bc.year == RATIO_CELL:
                got.append((page, bc))
    if not got:
        return None
    page, bc = got[0]
    cell_val, raw, label = bc.value, bc.raw, bc.label
    said = claim.value or 0.0
    # 方向词常写在前一个子句里（「营业收入 42.02 亿元，同比下降 1.77%」的第二种排布），
    # 所以这里看整句而不是只看断言子句。
    ctx_dir = _context_text(claim)
    if any(w in ctx_dir for w in ("下降", "减少", "下滑", "降低")):
        said = -abs(said)
    elif any(w in ctx_dir for w in ("增长", "增加", "上升", "提升")):
        said = abs(said)
    ev = [_ev(page, Token(value=cell_val, unit="%", base=None, raw=raw, page=page.page,
                          before=label[-40:], after=""),
             snippet(page, spec["aliases"][0], value_raw=raw))]
    if abs(abs(said) - abs(cell_val)) <= 0.05:
        if cell_val * said < 0:
            return Verdict(
                label=CONTRADICTED, error_type="numeric_mismatch", confidence=0.85,
                basis=(f"材料第 {page.page} 页「主要会计数据和财务指标」表列示「{spec['cn']}」"
                       f"本期比上年同期增减 {cell_val}%，主张写的是 {claim.value}%，"
                       f"绝对值一致但增/减方向相反。"),
                suggested_fix=f"应改为 {cell_val}%。",
                evidence=ev,
            )
        return Verdict(
            label=SUPPORTED, confidence=0.9,
            basis=(f"材料第 {page.page} 页「主要会计数据和财务指标」表列示「{spec['cn']}」"
                   f"本期比上年同期增减 {cell_val}%，与主张一致。"),
            evidence=ev,
        )
    return Verdict(
        label=CONTRADICTED, error_type="numeric_mismatch", confidence=0.85,
        basis=(f"主张给出 {claim.value}%，而材料第 {page.page} 页「主要会计数据和财务指标」"
               f"表列示「{spec['cn']}」本期比上年同期增减 {cell_val}%，两者不符。"),
        suggested_fix=f"应核对并改为 {cell_val}%（出处：第 {page.page} 页）。",
        evidence=ev,
    )


def _verify_claim_inner(claim: Claim, idx: MaterialIndex, *,
                        cutoff: str | None = None) -> Verdict:
    """数值比对本身（内层）：只回答「材料里对得上的数是哪个」，不决定「矛盾」能不能出。

    cutoff 是本案的预警截止日：主张所指期间落在它之后的，属预测/观点一类，
    材料里不可能有对应披露值，单列且不进核查分母。
    """
    if _out_of_scope(claim, cutoff):
        return _prediction(claim, cutoff or "")

    if claim.metric_key is None:
        return _insufficient("未能把该主张归到可核验的财务指标上，无法定位对应披露值。")
    spec = METRICS[claim.metric_key]
    if claim.value is None:
        return _insufficient("该主张未给出具体数值，无法与披露值逐项对照。")

    base_unit = spec["unit"]

    # 同比/增减率：主张单位是 % 而指标基准单位不是 %（如营业收入同比 +1.77%）。
    # 摘要表的「本期比上年同期增减(%)」列就是它的披露值，不能按绝对额换算。
    if (claim.unit or "").strip() == "%" and base_unit != "%":
        rv = _ratio_verdict(claim, spec, idx)
        if rv is not None:
            return rv

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
                if tok.value > 0 and _dividend_literal_ok(tok):
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
    # 期间统一在这里解析一次：字段优先、正文其次、都没有就按材料的报告期算（「本期」）。
    claim_per = _effective_claim_period(claim, idx)
    cands: list[tuple[PageRec, Token, float, bool]] = []   # (页, 候选, 归一值, 是否绑定得来)
    exact: tuple[PageRec, Token] | None = None
    for page in pages:
        # 摘要表上按「行 = 指标、列 = 期间」取候选：别的期间那一列不能给本期主张当证据，
        # 否则「预计 2025 年营收 45.2 亿」会命中 2021 年那一列的 45.12 亿判成「支持」，
        # 「2026 年上半年营收」也会命中年报里 2026 年整年那一列。
        # 行列表解析不出来时退回邻域取候选（老口径），解析得出来就用绑定值，避免
        # 把相邻行（另一个指标）的数字也当成这个指标的候选。
        bound = bound_cells(page, spec["aliases"])
        if not bound:
            # 行绑定绑不上时，试「指标在列头」的那种表（分行业表：行=行业、列=指标）
            bound = bound_column_cells(page, spec["aliases"], subject=_context_text(claim),
                                       metric_unit=spec.get("unit") or "")
        if not bound:
            # 有些 PDF 解析不出表格网格，只能按原始行（一格一行）绑列
            bound = bound_text_columns(page, spec["aliases"], subject=_context_text(claim),
                                       metric_unit=spec.get("unit") or "")
        if bound:
            pool: list[Token] = []
            for bc in bound:
                if bc.year == RATIO_CELL:
                    continue
                if not _column_matches_claim(page, bc.year, claim_per):
                    continue                      # 别的期间/口径那一列，不给本期主张作证据
                pool.append(Token(value=bc.value, unit=bc.unit, base=None, raw=bc.raw,
                                  page=page.page, before=bc.label[-40:], after=""))
        else:
            pool = tokens_near(page, spec["aliases"], base_unit=base_unit)
        for tok in pool:
            t_base = to_base(tok.value, tok.unit or base_unit, base_unit)
            if t_base is None:
                continue
            # 第四个元素记「这个候选是绑来的还是邻域捡来的」：绑来的才算「同类披露值」，
            # 邻域捡来的只能当比对候选，不能当修正值的出处。
            cands.append((page, tok, t_base, bool(bound)))
            if exact is None and _rel(t_base, v_base) <= TOL_EXACT:
                exact = (page, tok)

    def _dim_ok(t: Token) -> bool:
        u = t.unit
        if base_unit in ("%", "倍"):
            # 比率主张：材料那格标了 % 最好；只写数字（摘要表里很常见）也认，
            # 但数值必须像比率 —— 否则会把「营业收入 4,202,030,347.42」当成比率披露值。
            return u == base_unit or (u == "" and abs(t.value) <= _RATIO_PLAUSIBLE_MAX)
        if base_unit == "元/股":
            return u in ("", "元/股", "元")
        return u in ("", base_unit) or u in MONEY_SCALE

    def _quote(p: PageRec, t: Token | None = None) -> str:
        return snippet(p, spec["aliases"][0], value_raw=t.raw if t else None)

    # 口径核对要排在「精确命中」之前：口径错的主张，其数值本身往往是真的
    # （只是取自另一个口径的行），先判命中就会漏掉。
    if claim.metric_key == "weighted_roe":
        ctx = _context_text(claim)
        says_parent = "归母" in ctx or "普通股股东" in ctx
        says_deducted = "扣非" in ctx or "扣除非经常性损益" in ctx
        hit: tuple[PageRec, Token] | None = None
        if cands:
            page_t, tok_t = exact or (cands[0][0], cands[0][1])
            if _ROE_DEDUCT_LABEL in tok_t.before:
                hit = (page_t, tok_t)
            elif says_parent and not says_deducted and claim.value is not None:
                # 候选里没有扣非行（表格网格读不出、行标签被拆散）时，按「主张值取自哪一行」
                # 全材料找一遍：等值的那条出现在扣非行上，就是口径混用。
                for pg in pages:
                    t = _deducted_row_token(pg, claim_value=claim.value, base_unit=base_unit)
                    if t is not None:
                        hit = (pg, t)
                        break
        if hit and says_parent and not says_deducted:
            page, tok = hit
            others = [c[1] for c in cands
                      if _dim_ok(c[1]) and abs(c[1].value - tok.value) > 1e-9]
            ref = others[0] if others else None
            fix = (f"按归母净利润口径应为 {ref.value}%"
                   if ref else "按归母净利润口径应取未扣非的那一行")
            fix += "；若确要引用扣非口径，须在文中写明「扣除非经常性损益后」。"
            return Verdict(
                label=CONTRADICTED, error_type="caliber", confidence=0.8,
                basis=(f"主张写的是归母口径，但所引数值 {tok.value}% 出自材料中"
                       f"「扣除非经常性损益」那一行，属扣除非经常性损益后的口径，"
                       f"口径与数值不匹配。"),
                suggested_fix=fix,
                evidence=[_ev(page, tok, _quote(page, tok))],
            )

    # 主张引的那一页是不是也有这个值：同一数值常在多页出现（第 8 页摘要表、第 22 页
    # 经营情况讨论都写「营业收入 42.02 亿元」）。只认「首个命中页」会把**引对了**的
    # 主张判成引用页错误，所以先把「所有真有这个值的页」找出来，再看用户引的是不是其中之一。
    pages_with_value = {c[0].page for c in cands if _rel(c[2], v_base) <= TOL_EXACT}

    if exact is not None:
        page, tok = exact
        if claim.cited_page and claim.cited_page != page.page \
                and claim.cited_page not in pages_with_value:
            return Verdict(
                label=CONTRADICTED, error_type="citation_page", confidence=0.85,
                basis=(f"数值 {_fmt(tok.value)}{tok.unit} 本身正确，但主张把出处标为第 {claim.cited_page} 页；"
                       f"该值实际出现在第 {page.page} 页，第 {claim.cited_page} 页未出现该指标。"),
                suggested_fix=f"引用页码应改为第 {page.page} 页。",
                evidence=[_ev(page, tok, _quote(page, tok))],
            )

        # 材料内部自相矛盾：所引这一处不在「主要会计数据和财务指标」表上，而该表
        # 对同一指标给出的是另一个相差整十倍量级的值。原件抄错时正是这个形态
        # （天下秀 2023 年报第 22 页「0.74 万元」vs 第 8 页 73,672,369.59 元），
        # 此时判「支持」等于替原件背书。
        # 只认摘要表这一侧，是为了排除合并/母公司口径差异与附注明细拆解——那两种
        # 页面上出现的根本不是同一口径的同一披露，拿它们报矛盾就是误报。
        if not _is_primary_statement(page):
            for p2, t2, b2, _bound in cands:
                if not _is_primary_statement(p2):
                    continue
                if not _dim_ok(t2) or t2.value <= 0 or v_base <= 0:
                    continue
                if _rel(b2, v_base) <= TOL_EXACT:
                    continue                  # 与摘要表一致，只是重复披露
                ratio = b2 / v_base
                factors = [f for f in _SCALE_FACTORS if abs(ratio - f) / f <= TOL_SCALE]
                if not factors:
                    continue
                factor = max(factors)
                shown = factor if factor >= 1 else 1.0 / factor
                return Verdict(
                    label=CONTRADICTED, error_type="numeric_scale", confidence=0.8,
                    basis=(f"材料内部对「{spec['cn']}」存在两处互不相容的披露：所引第 {page.page} 页为 "
                           f"{_fmt(tok.value)}{tok.unit}，而第 {p2.page} 页「主要会计数据和财务指标」"
                           f"表为 {_fmt(t2.value)}{t2.unit or base_unit}，两者相差约 {shown:,.0f} 倍，"
                           f"不可能同时成立。主张与第 {page.page} 页字面一致，属照抄了其中一处表述。"),
                    suggested_fix=(f"以第 {p2.page} 页「主要会计数据和财务指标」表的 "
                                   f"{_fmt(t2.value)}{t2.unit or base_unit} 为准复核该表述。"),
                    evidence=[_ev(page, tok, _quote(page, tok)),
                              _ev(p2, t2, snippet(p2, spec["aliases"][0], value_raw=t2.raw))],
                )

        return Verdict(label=SUPPORTED, confidence=0.9,
                       basis=f"材料第 {page.page} 页披露 {_fmt(tok.value)}"
                             f"{tok.unit or base_unit}，与主张一致。",
                       evidence=[_ev(page, tok, _quote(page, tok))])

    if not cands:
        note = _period_mismatch_note(claim, idx)
        if note:
            return _insufficient(note)
        return _insufficient(
            f"材料里出现了「{spec['cn']}」，但其披露位置附近未取到可用数值，无法比对。")

    # 量级/单位型偏差：找比值最贴近某个 10^k 的候选，而不是「相对差最小」的候选。
    # 只看同维度的候选——否则 32.5% 会被拿去和 12.4 亿 比，算出一个荒唐的倍数。
    scaled: tuple[PageRec, Token, float, float] | None = None
    for page, tok, t_base, _bound in cands:
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
                   f"与材料第 {page.page} 页披露的 "
                   f"{_fmt_evidence_amount(tok.value, tok.unit, base_unit)} 相差{factor}。"),
            suggested_fix=(f"应改为 {_fmt_evidence_amount(tok.value, tok.unit, base_unit)}"
                           f"（出处：第 {page.page} 页）。"),
            evidence=[_ev(page, tok, _quote(page, tok))],
        )

    same_dim = [c for c in cands if _dim_ok(c[1]) and (base_unit != "%" or c[1].value >= 0)]
    if not same_dim:
        # 没有同口径候选时绝不能顺手拿第一个候选当「材料披露值」：那可能是另一个指标、
        # 另一个单位（「毛利率应改为 4,202,030,347.42%」就是这么来的）。宁可降级。
        return _insufficient(
            f"材料里没有取到与主张同口径（{base_unit}）的「{spec['cn']}」可比数值："
            f"该指标附近出现的候选都不是同口径的数值，无法据此给出修正值。")
    # 「同类披露值」只认绑来的候选：邻域捡来的数字只能参与比对，不能当「材料披露值」。
    # 绑不到单元格时仍然可以判「与披露不一致」（数值比对本身成立），但不给具体修正值 ——
    # 编一个修正值比不给更糟（「毛利率应改为 4,202,030,347.42%」就是这么来的）。
    bound_dim = [c for c in same_dim if c[3]]
    shown = (bound_dim or same_dim)[:4]
    page, tok, _b, _bound = shown[0]
    values = "、".join(_fmt_evidence_amount(c[1].value, c[1].unit, base_unit) for c in shown)
    if bound_dim:
        basis = (f"主张 {claim.value}{claim.unit}（归一 {_fmt(v_base)}{base_unit}）与材料第 {page.page} 页"
                 f"披露的 {_fmt_evidence_amount(tok.value, tok.unit, base_unit)} 不符；"
                 f"该页同类披露值为 {values}。")
        fix = (f"应核对并改为材料披露值（如 "
               f"{_fmt_evidence_amount(tok.value, tok.unit, base_unit)}，出处：第 {page.page} 页）。")
    else:
        basis = (f"主张 {claim.value}{claim.unit}（归一 {_fmt(v_base)}{base_unit}）与材料第 {page.page} 页"
                 f"出现的一处数值不符；这一页没有把「{spec['cn']}」按行/列绑定到可比单元格，"
                 f"附近出现的数值为 {values}（未绑定，仅作比对参考）。")
        fix = (f"材料里没有把「{spec['cn']}」绑定到可比单元格，不给具体修正值；"
               f"请对照第 {page.page} 页原文人工复核该数值。")
    return Verdict(
        label=CONTRADICTED, error_type="numeric_mismatch", confidence=0.75,
        basis=basis,
        suggested_fix=fix,
        evidence=[_ev(c[0], c[1], _quote(c[0], c[1])) for c in shown],
    )


def verify_claim(claim: Claim, idx: MaterialIndex, *,
                 cutoff: str | None = None,
                 allow_contradiction: bool = True,
                 block_reason: str | None = None) -> Verdict:
    """确定性判定（唯一对外入口）。全部是代码，没有模型参与。

    这一层的职责是证据资格门：先说清楚「这条主张有没有资格被拿来判矛盾」，再让内层
    做数值比对。「矛盾」是全项目最重的标签，必须严格证明；说不清就降级为「证据不足」——
    系统可以轻易说「暂时无法验证」，但不该轻易说「券商写错了」。
    """
    # 预测/观点用语优先于一切数值比对：「预计2026-2028年营收…」不是披露事实
    word = _is_forecast_text(claim)
    if word:
        return Verdict(
            label=PREDICTION, confidence=1.0,
            basis=(f"该主张带预测/观点用语「{word}」，不是对已发生事实的陈述，不参与历史事实"
                   f"核查，单列一类、不计入核查分母。"),
            unable_reason="属预测/观点（非披露事实），不参与数值核查。",
        )
    v = _verify_claim_inner(claim, idx, cutoff=cutoff)
    if v.label == SUPPORTED:
        # 「支持」也要过同一道证据资格门：说不清是哪个指标、哪一期，就不是证据，
        # 判「支持」等于替草稿背书（本年主张命中上一年数值就是这么来的）。
        why = _support_block(claim, idx)
        return _insufficient(why) if why else v
    if v.label != CONTRADICTED:
        return v
    why = _contradiction_block(claim, idx, allow_contradiction=allow_contradiction,
                               block_reason=block_reason)
    if why:
        return _insufficient(why)
    return v


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
        out = {SUPPORTED: 0, CONTRADICTED: 0, INSUFFICIENT: 0, PREDICTION: 0}
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


#: 引擎与提示词版本：它们的改动会影响结果，必须进运行编号 —— 事后要能回答
#: 「这条结论是哪一版引擎、哪一版提示词跑出来的」，回放才对得上。
ENGINE_VERSION = "fintrace-engine/0.4"
PROMPT_VERSION = "draft-split/v2"


def _file_fingerprint(path: "Path | str", *, root: "Path | None" = None) -> str:
    """材料原件的指纹：文件名 + 大小 + **内容 sha256**（分块读，不整份进内存）。

    只靠大小 + 修改时间不够，两次实测都不够：
      · 修改时间截到整秒时，同一秒内被覆盖的同尺寸文件认不出来；
      · 改成纳秒精度仍然不够 —— Windows 的文件时间戳粒度受系统时钟 tick（约 15.6ms）
        限制，同一个 tick 内改写、大小不变，mtime_ns 一模一样（P1-11 探针实测：
        AAAA → BBBB 指纹不变）。
    所以直接读内容算哈希：4MB 的年报也就几十毫秒，换文件、重新导出、内容被改都能认出来。
    内容读不到时（权限/占用）退回时间戳，宁可偶尔重算，不可漏判没变。
    """
    import hashlib

    p = Path(path or "")
    if root is not None and not p.is_absolute():
        p = root / p
    try:
        st = p.stat()
    except OSError:
        return "missing"
    h = hashlib.sha256()
    h.update(f"{p.name}|{st.st_size}".encode("utf-8"))
    try:
        with p.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        h.update(f"|mtime_ns:{st.st_mtime_ns}".encode("utf-8"))
    return h.hexdigest()[:10]


def _run_log_id(payload: dict, *, draft_text: str = "", documents: list | None = None) -> str:
    """运行编号覆盖**所有真正决定结果的东西**：案例 JSON、草稿正文、材料原件、引擎与提示词版本。

    只哈希案例 JSON 时，改了草稿、换了材料、重跑出新结果，编号都不变 —— 事后拿编号既
    对不上哪一次运行，离线回放也无从复核（反例已确认）。
    """
    import hashlib

    body = {
        "engine": ENGINE_VERSION,
        "prompt": PROMPT_VERSION,
        "case": payload,
        "draft_sha256": hashlib.sha256((draft_text or "").encode("utf-8")).hexdigest()[:16],
        "materials": [_file_fingerprint(str((d or {}).get("path") or ""), root=ROOT)
                      for d in (documents or [])],
    }
    h = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:12]
    return f"RUN-{h}"


def run_draft_case(payload: dict, *, use_llm: bool = True, cfg: dict | None = None,
                   claims_override: list[dict] | None = None,
                   claims_how: str = "",
                   on_event: Any = None) -> DraftReviewResult:
    """跑完整闭环。payload 见 data/annotations/cases/*.json。

    claims_override 用来复用「已经拆好、并且存过盘」的主张：大模型只参与一次，
    之后每次运行都用同一份主张，同一份草稿重跑结果逐字相同（可复现）。给了它就不再调模型。

    on_event：可选回调，收到过程中的真实进度（材料登记 / 建索引 / 逐条判定）。
    只用于把「正在干什么」透给界面，不参与任何判定，也不改变结果。
    """
    case_id = payload.get("case_id", "case")
    company = payload.get("company", "")
    cutoff = payload["prediction_cutoff_date"]
    draft_path = ROOT / payload["draft"]
    stages: dict[str, Any] = {}
    warnings: list[str] = []

    def emit(**ev: Any) -> None:
        if on_event is not None:
            on_event(ev)

    stages["stage0_run_log_id"] = ""      # 等草稿正文与材料都登记好再算（见下）

    # ① 主张拆解
    # 草稿是 PDF 转出来的硬折行文本，先接回断词换行再拆句：否则会切出
    # 「度报告，1H26 实现营业收入…」这种半截主张（模型路径同样受益）。
    draft_text = _unwrap_soft_wraps(draft_path.read_text(encoding="utf-8"))
    if claims_override is not None:
        raw_claims, how, degrade = [dict(c) for c in claims_override], (claims_how or "cached"), None
    else:
        raw_claims, how, degrade = split_claims(
            draft_text, cfg=cfg, use_llm=use_llm, source=str(draft_path.name))
    if degrade:
        warnings.append(degrade)
    _attach_sentence_ids(raw_claims, draft_text)
    stages["stage1_decomposition"] = {"method": how, "count": len(raw_claims)}
    claims = [_to_claim(c, i + 1) for i, c in enumerate(raw_claims)]
    emit(kind="claim_split", count=len(claims), how=how)

    # ② 材料登记 + 截止日过滤（硬门）
    sources, warn = dr.recognize_all(payload.get("documents", []))
    warnings.extend(warn)
    # 运行编号在这里定：此时草稿正文与材料清单都已到手，编号才真正覆盖本次运行的输入
    stages["stage0_run_log_id"] = _run_log_id(payload, draft_text=draft_text,
                                              documents=payload.get("documents") or [])
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
            warnings.append(cf.exclusion_note(sd.file_name, sd.publication_date, reasons, cutoff))
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
    # 证据资格门（案件级）：材料讲的年份和草稿讲的年份没有交集 → 逐条比对没有意义，
    # 这一案一律不出「矛盾」结论（材料是 2018 年报、草稿通篇 2026–2028 就是这种情形）。
    gap = _case_overlap_gate(page_recs, draft_text)
    # 材料窗口和草稿期间有没有交集：没有就先说清楚，免得逐条结论被当成对草稿对错的判断。
    coverage = _coverage_check(claims, cutoff)
    if gap:
        warnings.insert(0, gap)
        stages["stage2b_coverage"] = {"verdict": "no_overlap", "detail": gap}
    elif coverage:
        warnings.insert(0, coverage)
        stages["stage2b_coverage"] = {"verdict": "no_overlap", "detail": coverage}
    emit(kind="index", pages=len(page_recs), materials=len(allowed),
         allowed=[s.file_name for s in allowed], excluded=[e["file_name"] for e in excluded])

    # ③④⑤ 检索 → 判定 → 标签
    out: list[dict] = []
    for i, c in enumerate(claims):
        v = verify_claim(c, idx, cutoff=cutoff, allow_contradiction=not gap,
                         block_reason=gap)
        # 先查措辞、再发事件：违规原文一旦先发到界面上，就再也收不回来了。
        # 草稿里本来就写着定性断言时，那是「被核的对象」而不是我们的结论 ——
        # 系统不复述它（事件流、报告、对话一律只给中性说法），但要留一条告警说清哪里被隐去。
        shown, hit = wg.safe_text(c.claim_text)
        if hit is not None:
            warnings.append(f"{c.claim_id}：主张原文含{hit.why}，按措辞约束不在界面与报告里转述"
                            f"原文，只给判定结果（原文仍在草稿里，请自行核对）")
        emit(kind="claim", n=i + 1, of=len(claims), claim_id=c.claim_id,
             label=v.label, text=shown)
        per = _claim_period(c)
        assertion = wg.safe_text(c.assertion)[0] if c.assertion else (
            f"{METRICS[c.metric_key]['cn']} {c.value:g}{c.unit}"
            if c.metric_key and c.value is not None else shown)
        out.append({
            "claim_id": c.claim_id,
            "claim_text": shown,
            "assertion": assertion,
            "source_sentence_id": c.source_sentence_id,
            "claim_group_id": c.claim_group_id,
            "metric": METRICS[c.metric_key]["cn"] if c.metric_key else None,
            "metric_key": c.metric_key,
            "claim_value": c.value,
            "claim_unit": c.unit,
            "claim_period": c.period,
            "claim_period_end": per.isoformat() if per else None,
            "cited_page": c.cited_page,
            "caliber": c.raw.get("caliber") or "",
            "relation": c.raw.get("relation") or "",
            "source_span": c.raw.get("source_span"),
            "source_sentence": c.raw.get("source_sentence") or "",
            "decomposed_by": c.source,
            "verdict": v.to_dict(),
        })
    # 同一句话拆出多条断言时标出同组条数：报告里要能看出这是「同句 2 条」，
    # 而不是两条互相重复的主张。
    groups: dict[str, int] = {}
    for r in out:
        g = r["claim_group_id"]
        if g:
            groups[g] = groups.get(g, 0) + 1
    for r in out:
        r["group_size"] = groups.get(r["claim_group_id"], 1)
    stages["stage5_verdicts"] = {c["claim_id"]: c["verdict"]["label"] for c in out}
    # 「全部矛盾」本身就是系统异常信号：真研报极少整篇全错，先怀疑配置而不是研报
    anomaly = _all_contradicted_anomaly(out)
    if anomaly:
        warnings.insert(0, anomaly)
        stages["stage5b_anomaly"] = {"verdict": "all_contradicted", "detail": anomaly}
    # 规则兜底抽取（模型不可用时）会拆出碎片主张，这件事本身要说出来，不能默默出结论
    if how == "rules_fallback":
        warnings.append("本次主张由规则兜底抽取（模型不可用，拆句质量下降）：不带指标名或期间的"
                        "碎片已按证据不足处理；要逐条可信结论请在模型可用时重跑。")
        stages["stage1b_extract_fallback"] = True

    _assert_report_clean(out, warnings)      # 出场前的最后一关：整份报告一起过措辞约束
    res = DraftReviewResult(
        run_log_id=stages["stage0_run_log_id"], case_id=case_id, company=company,
        cutoff=cutoff, claims=out, excluded=excluded, warnings=warnings, stages=stages,
    )
    res.markdown = render_markdown(res)
    return res


def _assert_report_clean(claims: list[dict], warnings: list[str]) -> None:
    """报告出场前的最后一关：所有面向用户的自然语言字段一起过措辞约束。

    这条门原来挂在逐条循环里、而且排在 `emit` 之后 —— 违规原文已经先到界面上才报错。
    现在循环里先 `safe_text` 中性化再发事件，这里再对整份报告的字段做一次总检查：
    真有漏网的，就在生成报告之前抛错，不出半成品。
    """
    texts: list[str] = []
    for c in claims:
        v = c.get("verdict") or {}
        texts += [str(c.get("claim_text") or ""), str(c.get("assertion") or ""),
                  str(v.get("basis") or ""), str(v.get("suggested_fix") or ""),
                  str(v.get("unable_reason") or ""), str(v.get("missing_input") or "")]
        texts += [str(e.get("quote") or "") for e in (v.get("evidence") or [])]
    texts += [str(w) for w in warnings]
    label = ("RISK_SIGNAL_DETECTED"
             if any((c.get("verdict") or {}).get("label") == CONTRADICTED for c in claims)
             else "INSUFFICIENT_INFORMATION")
    wg.assert_clean(texts, mode="pre_hoc", has_confirmation=False, label=label)


def _clip_display(s: str, n: int = 160) -> str:
    """展示用截断：表格类原句可能有几千字，报告里只留开头。"""
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:n] + "…"


def render_markdown(res: "DraftReviewResult") -> str:
    s = res.summary()
    lines = [
        f"# 研报草稿核查报告　{res.company or res.case_id}",
        "",
        f"- 运行日志编号：{res.run_log_id}",
        f"- 预测截止日：{res.cutoff}",
        f"- 主张合计：{len(res.claims)} 条　支持 {s[SUPPORTED]} 条 / 矛盾 {s[CONTRADICTED]} 条 / "
        f"证据不足 {s[INSUFFICIENT]} 条 / 预测·观点 {s[PREDICTION]} 条（后者不进核查分母）",
        "",
    ]
    cov = (res.stages or {}).get("stage2b_coverage") or {}
    if cov.get("detail"):
        lines += [f"> **材料覆盖度告警**：{cov['detail']}", ""]
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
        lines.append(f"- 主张：{c.get('assertion') or c['claim_text']}")
        if c.get("assertion") and c["assertion"] != c["claim_text"]:
            lines.append(f"- 原句：{_clip_display(c['claim_text'])}")
        if (c.get("group_size") or 1) > 1:
            lines.append(f"- 同句断言：{c.get('claim_group_id') or '—'} 共 {c['group_size']} 条（同一句话拆出的多条断言，不是重复主张）")
        lines.append(f"- 判定依据：{v['basis']}")
        if v.get("suggested_fix"):
            lines.append(f"- 修改建议：{v['suggested_fix']}")
        if v.get("unable_reason") and v["unable_reason"] != v["basis"]:
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
    # 金标是否声明「已覆盖草稿全部可核验主张」（annotation_scope=exhaustive）。声明了才敢把
    # 运行时多报的错误裁决算成假阳性；没声明时金标本身可能漏标，那种条目只能列待审。
    gold_complete = str(exp.get("annotation_scope") or "").strip().lower() == "exhaustive"
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
        pick: tuple[tuple[int, float], int] | None = None
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
            # 期间、口径、关系也是「是不是同一条断言」的一部分：同一指标同一数值可能对应
            # 两条断言（两年同值、水平值与同比），只按数值挑会配错，再把本来对的记成判定错。
            pen = 0
            gper = str(g.get("period") or "")
            cper = str(c.get("period") or c.get("claim_period") or "")
            if gper and cper and gper != cper:
                pen += 1
            gcal = str(g.get("caliber") or "")
            ccal = str(c.get("caliber") or "")
            if gcal and ccal and gcal != ccal:
                pen += 1
            grel = str(g.get("relation") or "")
            crel = str(c.get("relation") or "")
            if grel and crel and grel != crel:
                pen += 1
            key = (pen, d)
            if pick is None or key < pick[0]:
                pick = (key, i)
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

    # 额外输出：运行时报出来、但真值里没有对应条目的主张。旧口径下它们不进任何统计，
    # 于是「多报一条错误裁决」可以零成本（实测反例：多一条错误裁决，FP 仍是 0）。
    # 这里单独记出来，并把「报成矛盾」的那部分明确标成多报的错误裁决。
    extra = [got[i] for i in range(len(got)) if i not in used]
    extra_err = [c for c in extra if c["verdict"]["label"] == CONTRADICTED]
    # 额外「报成矛盾」既不是漏报，也不是配对意义上的假阳性：它是引擎自己多判出来的错误裁决。
    # 金标声明完整 → 草稿里的主张金标都收过，多出来的只能是引擎错判，计入 FP 并参与失败判定；
    # 金标未声明完整 → 不能算 FP（可能是金标漏标），单列「待审」，但同样不能据此说「没有误报」。
    extra_as_fp = len(extra_err) if gold_complete else 0
    extra_needs_review = 0 if gold_complete else len(extra_err)
    fp += extra_as_fp
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    # 错误类型只对「真值也判成错误」的那些条算：真值标成 INSUFFICIENT 的条目没有错误类型
    # 可比（引擎对它的说明走 unable_reason），把它算进来只会造出一个假的「类型不一致」。
    err_rows = [r for r in rows if r["expected"] == CONTRADICTED]
    type_ok = sum(1 for r in err_rows if r["got_error_type"] == r["expected_error_type"])

    return {
        "scope": "case_level",
        "case_id": result.case_id,
        "gold_claims": n,
        "matched_claims": covered,
        "coverage": round(covered / n, 4) if n else 0.0,
        "runtime_claims": len(got),
        "label_accuracy": round(acc, 4),
        "gold_complete": gold_complete,
        "error_detection": {
            "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "fp_from_extra": extra_as_fp,
            "precision": round(prec, 4),
            "recall": round(rec, 4),
            "false_positive_rate": round(fp / (fp + tn), 4) if (fp + tn) else 0.0,
        },
        "extra_outputs": {
            "count": len(extra),
            "as_error": len(extra_err),
            "as_fp": extra_as_fp,
            "needs_review": extra_needs_review,
            "as_ok_or_other": len(extra) - len(extra_err),
            "items": [{"claim_id": c["claim_id"], "label": c["verdict"]["label"],
                       "metric_key": c.get("metric_key") or "",
                       "claim_text": str(c.get("claim_text") or "")[:60]} for c in extra[:10]],
            "note": ("额外输出＝真值里没有对应条目、运行时却报出来的主张。其中「报成矛盾」的"
                     "属于多报的错误裁决（既不是漏报，也不是配对意义上的假阳性）：金标声明完整"
                     "（annotation_scope=exhaustive）时计入 FP 并参与失败判定；金标未声明完整时"
                     "列为待审——两种情况都不能据此说「没有误报」。"),
        },
        "sections": {
            "抽取覆盖": {"金标条数": n, "匹配上": covered,
                         "覆盖率": round(covered / n, 4) if n else 0.0,
                         "运行时条数": len(got)},
            "额外输出": {"条数": len(extra), "其中报成矛盾": len(extra_err),
                         "计入FP": extra_as_fp, "待审": extra_needs_review},
            "标签": {"一致": sum(1 for r in rows if r["match"]), "总数": n,
                     "准确率": round(acc, 4)},
            "错误类型": {"一致": type_ok, "应报错误条数": len(err_rows),
                         "准确率": round(type_ok / len(err_rows), 4) if err_rows else None},
            "证据定位": {"命中": page_ok, "可核条数": page_tot,
                         "准确率": round(page_ok / page_tot, 4) if page_tot else None,
                         "口径": "严格：证据页是否落在真值页里（不含「同值多页」的放行）"},
        },
        "page_localization_accuracy": round(page_ok / page_tot, 4) if page_tot else None,
        "rows": rows,
        "note": "样本量小，仅案例级；不得外推为泛化准确率。",
    }
