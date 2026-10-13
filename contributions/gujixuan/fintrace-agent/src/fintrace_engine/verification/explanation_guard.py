"""模型解释的出场校验：数字必须能绑定到确定性结果里的一条事实。

措辞守门人管「不许下定性结论」；这一层管「不许说结果里没有的话」。做法不是拿数字字符串
去结果里对字面，而是两边都解析成**事实**再比：

    事实 = 带符号的值 + 单位 + 指标（认得出就绑）+ 出处（文件/页码）+ 来源

模型叙述里的每个数字都要命中一条事实，而且**单位、符号、指标**都得对得上：

  · 单位：42.02 亿元 与 42.02 万元 不是一回事（差 1 万倍），按单位换算后再比；
  · 符号：披露说「减少 3.73 个百分点」，写成「增加 3.73」不算命中；
  · 指标：句子点名了指标，就得用那个指标的事实，不许拿同页别的指标顶上；
  · 引用：页码必须属于所引材料，而且**那一页得真有你刚提到的这个数**（联合绑定）。

比对精度按模型自己写的小数位算（写 42.02 亿就按 0.01 亿的精度对齐 4,202,030,347.42 元），
没有一刀切的百分比容差；年份、计数这类没带单位的数字也必须在结果里有出处 ——
不做「小于 31 就放过」的通用豁免（那正是凭空写「毛利率 7%」能混过去的原因）。

**出处只认材料披露**。结果里另有两类数字，各自单独存放、默认不能当出处：

  · 草稿自己写的值（`claim_value` / 主张原文）——那是**待核对象**，不是事实。
    它写错了（草稿写「毛利率 21.75%」而材料是 18.02%），拿它当出处就等于替草稿背书。
    只有明确「引述草稿」的上下文（「草稿写的是…」「主张给出…」）才允许命中它；
  · 判定依据/suggested_fix 里的数字（`basis` 等）——那是我们自己的话，可能带着错值转述，
    一律不当出处（防止「判定依据里有 21.75%」反过来给 21.75% 发通行证）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

#: 模型解释没过校验时统一改用的开场白（确定性，不带任何结论）。
NEUTRAL_EXPLAIN_NOTE = "这次模型解释没过校验（数字或引用在确定性结果里找不到出处），我不转述它。"

#: 单位表：(写法, 基准单位键, 换算到基准单位的倍数)。长写法排前面，正则才吃得准。
UNITS: list[tuple[str, str, float]] = [
    ("万亿元", "元", 1e12), ("万亿", "元", 1e12), ("十亿", "元", 1e9), ("百万", "元", 1e6),
    ("亿元", "元", 1e8), ("万元", "元", 1e4), ("千元", "元", 1e3), ("元/股", "元/股", 1.0),
    ("个百分点", "%", 1.0), ("％", "%", 1.0), ("%", "%", 1.0), ("倍", "倍", 1.0),
    ("亿股", "股", 1e8), ("万股", "股", 1e4), ("股", "股", 1.0),
    ("元", "元", 1.0), ("条", "条", 1.0), ("家", "家", 1.0), ("个", "个", 1.0),
    ("份", "份", 1.0), ("页", "页", 1.0), ("名", "名", 1.0), ("天", "天", 1.0),
]
_UNIT_ALTERNATION = "|".join(u for u, _k, _s in UNITS)
_UNIT_INDEX = {u: (k, s) for u, k, s in UNITS}

_NUM = r"\d[\d,]*(?:\.\d+)?"
_MENTION_RX = re.compile(rf"(?P<sign>[-−－+])?\s*(?P<num>{_NUM})\s*(?P<unit>{_UNIT_ALTERNATION})?")
_PAGE_RX = re.compile(r"第\s*(\d+)\s*页")
_FILE_RX = re.compile(r"[^\s，。；、（）()「」【】：:]*\.(?:pdf|PDF|xlsx|xls|docx|doc|md|txt)")
_YEAR_RX = re.compile(r"(?:19|20)\d{2}")

#: 方向词：模型写「增加/减少」时，符号必须能从文字读出来才算命中
_SIGN_WORDS: list[tuple[str, int]] = [
    ("减少", -1), ("下降", -1), ("下滑", -1), ("降低", -1), ("亏损", -1), ("净流出", -1),
    ("负增长", -1), ("增加", 1), ("增长", 1), ("上升", 1), ("提高", 1), ("净流入", 1),
]
#: 不带单位的数字只认这几类事实（条数、家数、页码这类计数）
_COUNT_UNITS = ("条", "家", "个", "份", "页", "名", "天")
#: 披露类单位：只有这几类数字参与「页码 ↔ 数值」的联合绑定（计数不算披露）
_DISCLOSURE_UNITS = ("元", "%", "倍", "元/股", "股", "元/10股")

#: 「引述草稿」的上下文标记：必须**点名草稿/主张**才算引述。
#: 不许把「给出 / 原文 / 标注」这类泛词算进来 —— 「年报给出毛利率 21.75%」正是靠它们
#: 把草稿的错值伪装成材料披露（实测绕过）。「原文」在年报场景里指的往往就是材料本身，
#: 更不能当作引述草稿的证据。
_DRAFT_QUOTE_RX = re.compile(r"草稿|主张")
#: 子句边界：引述标记必须与数字同处一个子句（「草稿写的是 X」算，「草稿里提到营业收入，
#: 毛利率 21.75%」不算 —— 中途已经换了一个子句/一个指标）。
_CLAUSE_CUT_RX = re.compile(r"[，,。；;！？!?\n、]")

#: 事实来源
MATERIAL = "material"      # 材料披露（evidence 的 value/quote）—— 唯一可以当出处的
CLAIM = "claim"            # 草稿自己写的值（待核对象）
ENGINE = "engine"          # 本系统判定依据里的数字（可能转述错值）


@dataclass(frozen=True)
class Fact:
    """一条事实：带符号的值 + 基准单位 + 指标 + 出处 + 来源。"""

    value: float              # 已按单位换算到基准单位，带符号
    unit: str                 # 基准单位键：元 / % / 倍 / 元/股 / 条 …
    metric: str = ""          # 指标 key（空 = 结果里没给指标）
    source: str = ""          # 出处说明（文件/页码/字段），只用于写问题描述
    sign_from_text: bool = False   # 符号是从文字（减少/亏损…）读出来的
    origin: str = MATERIAL    # 见 MATERIAL / CLAIM / ENGINE
    page: int | None = None   # 材料页码（材料事实才有）
    file_name: str = ""       # 材料文件名（材料事实才有）


@dataclass(frozen=True)
class Mention:
    """模型叙述里的一个数字。"""

    raw: str
    value: float
    unit: str | None
    scale: float
    decimals: int
    metric: str
    sign_word: str
    start: int        # 数字本身在原文里的起始下标（不是整个匹配的起点）


@dataclass
class Corpus:
    """一份确定性结果里能拿来当出处的全部东西。"""

    material: list[Fact] = field(default_factory=list)       # 可以当出处的材料事实
    claim: list[Fact] = field(default_factory=list)          # 草稿自己写的值（待核对象）
    pages: dict[str, set[int]] = field(default_factory=dict)  # 文件 → 出现过证据的页
    page_facts: dict[tuple[str, int], list[Fact]] = field(default_factory=dict)
    files: list[str] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)           # 年份/条数这类弱出处
    excluded: set[str] = field(default_factory=set)


def _canon_unit(unit: str) -> tuple[str, float] | None:
    return _UNIT_INDEX.get((unit or "").strip().replace("％", "%"))


def _metric_key(name: str) -> str:
    """把指标名（中文名或 key）归一到项目里的指标 key。"""
    name = (name or "").strip()
    if not name:
        return ""
    try:
        from ..draft_review import METRICS, resolve_metric   # 局部导入：避免模块级循环依赖
    except Exception:                                        # pragma: no cover
        return ""
    if name in METRICS:
        return name
    for key, spec in METRICS.items():
        if spec.get("cn") == name:
            return key
    return resolve_metric(name) or ""


def _sign_of(text: str, start: int, explicit: str) -> tuple[int, str]:
    """这个数字的符号：先看它自己带的负号，再看紧邻前面几个字里的方向词。"""
    if explicit in ("-", "−", "－"):
        return -1, explicit
    window = text[max(0, start - 8):start]
    for word, sign in _SIGN_WORDS:
        pos = window.rfind(word)
        if pos >= 0 and pos + len(word) >= len(window) - 1:
            return sign, word
    if explicit == "+":
        return 1, explicit
    return 1, ""


def _mentions(text: str) -> list[Mention]:
    out: list[Mention] = []
    text = text or ""
    for m in _MENTION_RX.finditer(text):
        num = m.group("num")
        canon = _canon_unit(m.group("unit")) if m.group("unit") else None
        sign, word = _sign_of(text, m.start(), m.group("sign") or "")
        try:
            val = float(num.replace(",", "")) * sign
        except ValueError:                                   # pragma: no cover - 正则保证可转
            continue
        window = text[max(0, m.start("num") - 24):m.end() + 4]
        out.append(Mention(
            raw=num, value=val,
            unit=canon[0] if canon else None,
            scale=canon[1] if canon else 1.0,
            decimals=len(num.split(".")[1]) if "." in num else 0,
            metric=_metric_key(window), sign_word=word, start=m.start("num")))
    return out


def _text_facts(text: str, *, metric: str, source: str, origin: str,
                page: int | None = None, file_name: str = "",
                default_unit: tuple[str, float] | None = None) -> list[Fact]:
    """把一段文本里的数字抽成事实（材料原文、草稿主张、判定依据各按来源登记）。

    default_unit：这段文本自己没写单位时按什么单位算（材料引文里的裸数字按该条证据声明的
    单位计；没有声明就跳过 —— 宁可少一条事实，也不给裸数字猜个单位）。
    """
    out: list[Fact] = []
    for m in _mentions(text or ""):
        unit, scale = m.unit, m.scale
        if unit is None:
            if default_unit is None:
                continue
            unit, scale = default_unit
        out.append(Fact(value=m.value * scale, unit=unit, metric=metric or m.metric,
                        source=source, sign_from_text=bool(m.sign_word) or m.value < 0,
                        origin=origin, page=page, file_name=file_name))
    return out


def _walk_claims(obj: dict, corpus: Corpus) -> None:
    for c in obj.get("claims") or []:
        if not isinstance(c, dict):
            continue
        cid = str(c.get("claim_id") or "?")
        key = _metric_key(str(c.get("metric") or c.get("metric_key") or ""))
        # 草稿主张原文与主张值：待核对象，单独登记
        for fld in ("claim_text", "assertion"):
            if c.get(fld):
                corpus.texts.append(str(c[fld]))
                corpus.claim += _text_facts(str(c[fld]), metric=key,
                                            source=f"{cid}.{fld}", origin=CLAIM)
        val, unit = c.get("claim_value"), _canon_unit(str(c.get("claim_unit") or ""))
        if isinstance(val, (int, float)) and unit:
            corpus.claim.append(Fact(value=float(val) * unit[1], unit=unit[0], metric=key,
                                     source=f"{cid}.claim_value", origin=CLAIM))
        v = c.get("verdict") or {}
        # 判定依据/suggested_fix：是我们自己的话，只进 texts（供年份这类弱出处），不当事实
        for fld in ("basis", "unable_reason", "suggested_fix"):
            if v.get(fld):
                corpus.texts.append(str(v[fld]))
        ev_files: list[str] = []
        for e in (v.get("evidence") or []):
            fn = str(e.get("file_name") or "")
            pg = e.get("page") if isinstance(e.get("page"), int) else None
            if fn:
                ev_files.append(fn)
                if fn not in corpus.files:
                    corpus.files.append(fn)
                if pg is not None:
                    corpus.pages.setdefault(fn, set()).add(int(pg))
            # evidence 里的 value/quote 才是材料披露 → 唯一能当出处的一类
            ev_val, ev_unit = e.get("value"), _canon_unit(str(e.get("unit") or ""))
            if isinstance(ev_val, (int, float)) and ev_unit:
                f = Fact(value=float(ev_val) * ev_unit[1], unit=ev_unit[0], metric=key,
                         source=f"{fn or '材料'} 第 {pg} 页", origin=MATERIAL,
                         page=pg, file_name=fn)
                corpus.material.append(f)
                if pg is not None:
                    corpus.page_facts.setdefault((fn, int(pg)), []).append(f)
            if e.get("quote"):
                corpus.texts.append(str(e["quote"]))
                # 引文里的裸数字不按证据单位硬套：一张分行业表的引文里金额与比率混着，
                # 套上「%」会把 4,202,030,347.42 变成「4,202,030,347.42%」。带单位的照收，
                # 不带单位的只当弱出处；材料披露值以该条证据的 value+unit 为准。
                qfacts = _text_facts(str(e["quote"]), metric=key,
                                     source=f"{fn or '材料'} 第 {pg} 页 引文", origin=MATERIAL,
                                     page=pg, file_name=fn)
                corpus.material += qfacts
                if pg is not None:
                    corpus.page_facts.setdefault((fn, int(pg)), []).extend(qfacts)
        if isinstance(c.get("cited_page"), int):
            for fn in (ev_files or corpus.files or ["（材料未定）"]):
                corpus.pages.setdefault(fn, set()).add(int(c["cited_page"]))


def _facts_from_result(blob: str) -> Corpus:
    """从确定性结果里抽出：材料事实、草稿值、页码表、文件名清单、全文与弱出处。"""
    corpus = Corpus()
    try:
        obj = json.loads(blob)
    except (json.JSONDecodeError, TypeError):
        obj = None
    if isinstance(obj, dict):
        _walk_claims(obj, corpus)
        for d in (obj.get("documents") or []):
            name = d.get("file_name") if isinstance(d, dict) else d
            if name and str(name) not in corpus.files:
                corpus.files.append(str(name))
        for d in (obj.get("excluded_documents") or []) + (obj.get("excluded") or []):
            name = d.get("file_name") if isinstance(d, dict) else d
            if name:
                corpus.excluded.add(str(name))
                if str(name) not in corpus.files:
                    corpus.files.append(str(name))
        summary = obj.get("summary") or {}
        if isinstance(summary, dict):
            for label, n in summary.items():
                if isinstance(n, (int, float)):
                    corpus.material.append(Fact(value=float(n), unit="条",
                                                source=f"summary.{label}", origin=ENGINE))
        if obj.get("claims"):
            corpus.material.append(Fact(value=float(len(obj["claims"])), unit="条",
                                        source="主张条数", origin=ENGINE))
        for key in ("prediction_cutoff_date", "cutoff", "run_log_id", "case_id", "company"):
            if obj.get(key):
                corpus.texts.append(str(obj[key]))
        for w in (obj.get("warnings") or []):
            corpus.texts.append(str(w))
    else:
        # 纯文本结果（CLI 兜底）：没有结构化出处，只能整体当材料文本用
        corpus.texts.append(blob)
        corpus.material += _text_facts(blob, metric="", source="结果文本", origin=MATERIAL)
        for m in _FILE_RX.finditer(blob):
            name = m.group(0).strip()
            if name and name not in corpus.files:
                corpus.files.append(name)
    return corpus


def _matches_value(m: Mention, f: Fact) -> bool:
    """按模型自己写的精度比：写 42.02 亿就按 0.01 亿的精度对齐 4,202,030,347.42 元。"""
    if (m.value < 0) != (f.value < 0):
        return False
    target = f.value / m.scale
    return abs(round(target, m.decimals) - m.value) <= 0.5 * 10 ** (-m.decimals) + 1e-9


def _match_mention(m: Mention, corpus: Corpus, text: str = "",
                   ordinal_max: float = 0.0) -> str:
    """这个数字能不能命中一条事实：能就返回空串，不能就返回说明。"""
    plain = m.raw.replace(",", "")
    if m.unit in (None, "条") and text[:m.start].rstrip().endswith("第"):
        # 「第 3 条」是序号：只要不超出结果里的条数就算有出处（「第 999 条」就超了）
        if float(plain) <= ordinal_max:
            return ""
        return f"序号「第 {m.raw} 条」超出了本次结果的范围（最多 {ordinal_max:g} 条）"
    if m.unit is None:
        # 不带单位：年份要真在结果里出现过；其余只能命中计数类事实。
        # 不做「小整数一律放过」的通用豁免 —— 那正是凭空写「毛利率 7%」混过去的原因。
        if _YEAR_RX.fullmatch(plain):
            return "" if any(plain in t for t in corpus.texts) else f"年份 {m.raw} 在确定性结果里找不到出处"
        cands = [f for f in corpus.material + corpus.claim if f.unit in _COUNT_UNITS]
        if any(_matches_value(m, f) for f in cands):
            return ""
        return f"数字 {m.raw} 在确定性结果里找不到出处"
    cands = [f for f in corpus.material if f.unit == m.unit]
    if m.metric:
        cands = [f for f in cands if not f.metric or f.metric == m.metric]
    if m.sign_word:                     # 句子写了方向，就得有从文字读出来的同向事实
        cands = [f for f in cands if f.sign_from_text]
    if any(_matches_value(m, f) for f in cands):
        return ""
    # 草稿自己写的值不是事实：只有在**同一个子句里点明「草稿/主张」**时才允许命中
    if _draft_quote_before(text, m.start):
        if any(_matches_value(m, f) for f in corpus.claim if f.unit == m.unit):
            return ""
    other = [f for f in corpus.material if f.unit != m.unit and _matches_value(m, f)]
    if other:
        return (f"数字 {m.raw}{m.unit} 的单位对不上：结果是 {other[0].value:g}{other[0].unit}"
                f"（{other[0].source}）")
    flipped = Mention(m.raw, -m.value, m.unit, m.scale, m.decimals, m.metric, "", m.start)
    if any(_matches_value(flipped, f) for f in corpus.material if f.unit == m.unit):
        return f"数字 {m.raw}{m.unit} 的符号与披露相反：披露是 {-m.value:g}{m.unit}"
    return f"数字 {m.raw}{m.unit} 在确定性结果里找不到出处"


def _resolve_file(name: str, files: list[str]) -> str:
    """模型可能只写简称：它和结果里的文件名互为子串就算同一份。"""
    if not name:
        return ""
    stem = name.rsplit(".", 1)[0]
    for f in files:
        if name in f or f in name or (len(stem) >= 4 and stem in f):
            return f
    return ""


def _named_file_before(text: str, pos: int, files: list[str]) -> str:
    """页码前面提到的是哪份材料：文件名可能没写后缀（「见更正公告第 8 页」）。"""
    window = text[max(0, pos - 40):pos]
    fm = None
    for fm in _FILE_RX.finditer(window):
        pass
    if fm is not None:
        named = _resolve_file(fm.group(0).strip(), files)
        if named:
            return named
    for f in files:
        stem = f.rsplit(".", 1)[0]
        for size in (12, 8, 6, 4):                 # 长片段优先，避免把「报告」当文件名
            for i in range(0, max(0, len(stem) - size + 1)):
                piece = stem[i:i + size]
                if piece in window and not piece.isdigit():
                    return f
    return ""


def _sentence_span(text: str, pos: int) -> tuple[int, int]:
    """模型这句话里 pos 所在的句子（只按句末与分号切，逗号不切）。"""
    cuts = {0, len(text)}
    for m in re.finditer(r"[。！？!?\n；;]", text):
        cuts.add(m.start())
        cuts.add(m.end())
    cuts = sorted(cuts)
    for a, b in zip(cuts, cuts[1:]):
        if a <= pos < b:
            return a, b
    return 0, len(text)


def _draft_quote_before(text: str, pos: int) -> bool:
    """这个数字前面、**同一个子句内**有没有点明「草稿 / 主张」。

    比「往前看 12 个字里有没有某个词」严：一是只认草稿/主张这两个主体词，
    二是中途换了子句（逗号/分号/顿号）就不再算 —— 否则「草稿里营业收入 42.02 亿元，
    毛利率 21.75%」会把后半句的草稿值连坐放行。
    """
    cut = 0
    for m in _CLAUSE_CUT_RX.finditer(text[:pos]):
        cut = m.end()
    return bool(_DRAFT_QUOTE_RX.search(text[cut:pos]))


def _page_has_value(corpus: Corpus, named: str, page: int,
                    nums: list[Mention]) -> Fact | None:
    """这一页（这份文件）有没有这些数里的某一个 —— 回查的是材料披露事实。"""
    for (fn, pg), fs in corpus.page_facts.items():
        if pg != page or (named and fn != named):
            continue
        for f in fs:
            if any(_matches_value(m, f) for m in nums):
                return f
    return None


def _where_value_is(corpus: Corpus, named: str, nums: list[Mention]) -> int | None:
    for (fn, pg), fs in corpus.page_facts.items():
        if named and fn != named:
            continue
        for f in fs:
            if any(_matches_value(m, f) for m in nums):
                return pg
    return None


def _check_citations(text: str, corpus: Corpus, mentions: list[Mention]) -> list[str]:
    """引用要和材料对应：文件名要在本次材料里，页码要有依据。

    页码不能只「属于这份文件」就算对，也不能拿**整句的数字池**一起兜 —— 后一种会让
    「营业收入 42.02 亿元见第 8 页，另外，我们核查发现毛利率 18.02% 也见第 8 页」过关：
    第 8 页确实有 42.02 亿元，于是第二个引用被顺带放行（实测绕过）。
    正确做法是**逐个引用各自绑定**：每个「第 N 页」只管它自己那段（上一个引用之后到它
    之前）出现的披露类数字。材料页码表为空（纯文本结果）时退回弱校验。
    """
    problems: list[str] = []
    known = set(corpus.pages)
    prev_end = 0
    for pm in _PAGE_RX.finditer(text):
        page = int(pm.group(1))
        named = _named_file_before(text, pm.start(), corpus.files)
        s_lo, s_hi = _sentence_span(text, pm.start())
        lo = max(s_lo, prev_end)          # 本引用自己的那段：上一个「第 N 页」之后
        prev_end = pm.end()
        if named and named in corpus.excluded:
            problems.append(f"引用的是没纳入本次判定的材料：{named.rsplit('.', 1)[0]}"
                            f"（被时间过滤排除或未参与核查），不能引它的页码")
            continue
        nums = [m for m in mentions
                if lo <= m.start < pm.start() and m.unit in _DISCLOSURE_UNITS]
        if not nums:
            # 这个引用的前面没有数字（「见第 8 页」单独成句）：退回整句，至少不放过明显不存在的页
            nums = [m for m in mentions
                    if s_lo <= m.start < s_hi and m.unit in _DISCLOSURE_UNITS]
        if nums and corpus.page_facts:
            if _page_has_value(corpus, named, page, nums) is None:
                where = _where_value_is(corpus, named, nums)
                near = "".join(f"{m.raw}{m.unit or ''}" for m in nums[:2])
                suffix = f"（该披露在第 {where} 页）" if where else ""
                problems.append(f"引用页码与数值对不上：第 {page} 页没有 {near}{suffix}")
            continue
        if named:
            if corpus.pages.get(named) and page not in corpus.pages[named]:
                # 只有真知道这份文件的页码时才拦：纯文本结果里没有页码信息，就没有依据
                problems.append(f"引用页码对不上：{named} 里没有第 {page} 页")
            continue
        if len(known) == 1 and len(corpus.files) == 1:
            only = next(iter(known))
            if page not in corpus.pages[only]:
                problems.append(f"引用页码对不上：{only} 里没有第 {page} 页")
        elif known and not any(page in s for s in corpus.pages.values()):
            problems.append(f"引用页码 {page} 在任何材料里都找不到")
    for m in _FILE_RX.finditer(text):
        name = m.group(0).strip()
        if name and not _resolve_file(name, corpus.files):
            problems.append(f"引用 {name} 不在本次材料清单里")
    return problems


def check(text: str, *, result_blob: str) -> list[str]:
    """模型解释里对不上出处的地方；空清单 = 数字与引用都能在确定性结果里找到。

    Args:
        text: 模型讲的这段人话。
        result_blob: 确定性结果（JSON 或报告文本），作为「出处」的唯一依据。
    """
    if not (text or "").strip():
        return []
    corpus = _facts_from_result(result_blob)
    ordinal_max = max((f.value for f in corpus.material if f.unit == "条"), default=0.0)
    spans = [(m.start(), m.end()) for m in _FILE_RX.finditer(text)]
    mentions = _mentions(text)
    problems: list[str] = []
    for m in mentions:
        if m.unit == "页":
            continue                     # 「第 N 页」当引用处理，下面专门查文件与页码
        if any(a <= m.start < b for a, b in spans):
            continue                     # 数字在文件名里（600556_2023年年度报告.pdf），不是叙述数字
        note = _match_mention(m, corpus, text, ordinal_max)
        if note:
            problems.append(note)
    problems.extend(_check_citations(text, corpus, mentions))
    return problems


def safe_explanation(text: str, *, result_blob: str) -> tuple[str, list[str]]:
    """模型解释出场前过一遍：对不上出处就不转述，换成中性说明。

    返回 (可安全输出的文本, 问题清单)。问题清单非空时返回的已是中性说明；
    调用方可以说清有几处对不上，但不要复述模型原文 —— 复述等于把编的又发一遍。
    """
    problems = check(text, result_blob=result_blob)
    if not problems:
        return text or "", []
    return (f"{NEUTRAL_EXPLAIN_NOTE}（{len(problems)} 处数字或引用在确定性结果里查不到对应项）",
            problems)
