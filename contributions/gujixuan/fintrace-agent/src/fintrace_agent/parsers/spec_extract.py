"""声明式抽取规格（v2.2 §5.4）：LLM 不写代码，只写规格，由已审的引擎解释执行。

这是「运行时工具自举」的过渡路径。规格是安全边界的一部分：
- 算子白名单：``locate.kind ∈ {paragraph, table_row, regex_in_paragraph}``、
  ``extract.kind ∈ {text, group}``、``group.index`` 必须是整数；
- 字段名白名单：``claim_text/value/unit/period/metric/row_label/page/quote``；
- 任何 ``import``/``exec``/``eval``/``open``/``subprocess``/``socket`` 字样、``__`` 痕迹一律拒绝。

``validate_spec`` 返回错误列表（空即通过）；``run_spec`` 先校验，**校验失败一律抛 ValueError 拒绝执行**。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from ..contracts import ExtractorTier, Span, SpanKind
from .bridge import decimals_of

#: 允许的字段名（规格里 ``fields[].name`` 只能取这些）
FIELD_WHITELIST = frozenset(
    {"claim_text", "value", "unit", "period", "metric", "row_label", "page", "quote"}
)
#: 定位算子白名单
LOCATE_KINDS = frozenset({"paragraph", "table_row", "regex_in_paragraph"})
#: 抽取算子白名单
EXTRACT_KINDS = frozenset({"text", "group"})
#: 允许的顶层键
TOP_KEYS = frozenset({"spec_id", "target", "fields", "unit_hint", "period_hint", "notes", "version"})
#: 危险字样：任何字符串里出现即拒绝（大小写不敏感）
DANGEROUS = ("import", "exec", "eval", "open", "subprocess", "socket")

#: 允许的字段键
_FIELD_KEYS = frozenset({"name", "locate", "extract"})


# ---------------------------------------------------------------------------
# 静态校验
# ---------------------------------------------------------------------------


def _iter_strings(obj, path: str = "spec"):
    """递归遍历，产出 (路径, 字符串) —— 用于危险字样扫描。"""
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _iter_strings(v, f"{path}.{k}")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            yield from _iter_strings(v, f"{path}[{i}]")


def _validate_locate(tag: str, locate) -> list[str]:
    errs: list[str] = []
    if not isinstance(locate, dict):
        return [f"{tag}.locate 必须是对象"]
    kind = locate.get("kind")
    if kind not in LOCATE_KINDS:
        errs.append(f"{tag}.locate.kind={kind!r} 不是允许的算子（{sorted(LOCATE_KINDS)}）")
    else:
        if kind in ("paragraph", "table_row"):
            c = locate.get("contains")
            if not isinstance(c, list) or not c or not all(isinstance(x, str) and x for x in c):
                errs.append(f"{tag}.locate.contains 必须是非空字符串列表")
            if "pattern" in locate:
                errs.append(f"{tag}.locate.pattern 只允许用于 regex_in_paragraph")
        elif kind == "regex_in_paragraph":
            pat = locate.get("pattern")
            if not isinstance(pat, str) or not pat:
                errs.append(f"{tag}.locate.pattern 必须是非空字符串")
            else:
                try:
                    re.compile(pat)
                except re.error as exc:
                    errs.append(f"{tag}.locate.pattern 不是合法正则：{exc}")
            if "contains" in locate:
                errs.append(f"{tag}.locate.contains 只允许用于 paragraph/table_row")
    for k in locate:
        if k not in ("kind", "contains", "pattern"):
            errs.append(f"{tag}.locate 含未知键 {k!r}")
    return errs


def _validate_extract(tag: str, extract, locate) -> list[str]:
    errs: list[str] = []
    if not isinstance(extract, dict):
        return [f"{tag}.extract 必须是对象"]
    kind = extract.get("kind")
    is_regex = isinstance(locate, dict) and locate.get("kind") == "regex_in_paragraph"
    if kind not in EXTRACT_KINDS:
        errs.append(f"{tag}.extract.kind={kind!r} 不是允许的算子（{sorted(EXTRACT_KINDS)}）")
    elif kind == "group":
        gi = extract.get("index")
        if not isinstance(gi, int) or isinstance(gi, bool):
            errs.append(f"{tag}.extract.index 必须是整数")
        elif gi < 0:
            errs.append(f"{tag}.extract.index 不能为负")
        if not is_regex:
            errs.append(f"{tag}.extract.kind=group 只能配 regex_in_paragraph 定位")
    allow = {"kind", "index"} if kind == "group" else {"kind"}
    for k in extract:
        if k not in allow:
            errs.append(f"{tag}.extract 含未知键 {k!r}")
    return errs


def _validate_field(i: int, f) -> list[str]:
    tag = f"fields[{i}]"
    if not isinstance(f, dict):
        return [f"{tag} 必须是对象"]
    errs: list[str] = []
    name = f.get("name")
    if name not in FIELD_WHITELIST:
        errs.append(f"{tag}.name={name!r} 不在字段白名单内（{sorted(FIELD_WHITELIST)}）")
    locate = f.get("locate")
    errs += _validate_locate(tag, locate)
    errs += _validate_extract(tag, f.get("extract"), locate)
    for k in f:
        if k not in _FIELD_KEYS:
            errs.append(f"{tag} 含未知键 {k!r}")
    return errs


def validate_spec(spec: dict) -> list[str]:
    """校验抽取规格，返回错误列表（空即通过）。任何一条错误都意味着不得执行。"""
    if not isinstance(spec, dict):
        return ["规格必须是 JSON 对象"]

    errs: list[str] = []

    # 1) 危险字样全量扫描（字段名、正则、任何字符串）
    for path, s in _iter_strings(spec):
        low = s.lower()
        for word in DANGEROUS:
            if word in low:
                errs.append(f"{path} 含被禁字样 {word!r}")
        if "__" in s:
            errs.append(f"{path} 含 dunder（__）痕迹")

    # 2) spec_id
    if not isinstance(spec.get("spec_id"), str) or not spec.get("spec_id"):
        errs.append("缺少 spec_id（必须是非空字符串）")

    # 3) 顶层未知键
    for k in spec:
        if k not in TOP_KEYS:
            errs.append(f"顶层含未知键 {k!r}")

    # 4) fields 与逐字段
    fields = spec.get("fields")
    if not isinstance(fields, list) or not fields:
        errs.append("fields 必须是非空列表")
        return errs
    for i, f in enumerate(fields):
        errs += _validate_field(i, f)
    return errs


# ---------------------------------------------------------------------------
# 解释执行
# ---------------------------------------------------------------------------


def _line_spans(text: str):
    """物理行 → (start, end, line_text)（偏移相对整页文本，跨行严格递增）。"""
    out = []
    pos = 0
    for line in text.split("\n"):
        out.append((pos, pos + len(line), line))
        pos += len(line) + 1
    return out


def _paragraphs(text: str):
    """按空行切段 → (start, end, para_text)，偏移相对整页文本。"""
    out = []
    cur_start = cur_end = None
    buf: list[str] = []

    def _flush():
        nonlocal cur_start, cur_end, buf
        if cur_start is not None:
            out.append((cur_start, cur_end, "\n".join(buf)))
        cur_start = cur_end = None
        buf = []

    for start, end, line in _line_spans(text):
        if line.strip():
            if cur_start is None:
                cur_start = start
            cur_end = end
            buf.append(line)
        else:
            _flush()
    _flush()
    return out


def _first_cell(line: str) -> str:
    """表格行取「第一个单元格」文本：按制表符/连续空格/竖线切。"""
    parts = re.split(r"\t+|\s{2,}|\|", line.strip())
    return parts[0].strip() if parts else ""


def _iter_units(locate: dict, text: str):
    """按定位算子产出候选单元 dict：pstart/start/end/text/m/row_label。"""
    kind = locate["kind"]
    if kind in ("paragraph", "table_row"):
        contains = ["".join(c.split()) for c in locate["contains"]]
        units = _paragraphs(text) if kind == "paragraph" else [u for u in _line_spans(text) if u[2].strip()]
        for start, end, unit in units:
            flat = "".join(unit.split())
            if all(c in flat for c in contains):
                yield {
                    "start": start, "end": end, "text": unit, "m": None,
                    "row_label": _first_cell(unit) if kind == "table_row" else None,
                }
    else:  # regex_in_paragraph
        pat = re.compile(locate["pattern"])
        for start, end, unit in _paragraphs(text):
            for m in pat.finditer(unit):
                yield {
                    "start": start + m.start(), "end": start + m.end(),
                    "text": m.group(0), "m": m, "row_label": None,
                }


def run_spec(spec: dict, pages: list[dict]) -> list[Span]:
    """按规格从逐页文本里抽 Span。``pages`` 形如 ``[{"page": int, "text": str}]``。

    先 ``validate_spec``；**校验失败一律抛 ValueError，不执行**。
    ``quoted_text`` 原样保留抽到的原文（精度保真的 raw），``decimals_as_disclosed`` 由此判定。
    """
    errs = validate_spec(spec)
    if errs:
        raise ValueError("抽取规格校验失败，拒绝执行：" + "；".join(errs))

    unit_hint = spec.get("unit_hint") if isinstance(spec.get("unit_hint"), str) else ""
    period_hint = spec.get("period_hint") if isinstance(spec.get("period_hint"), str) else ""

    spans: list[Span] = []
    idx = 0
    for f in spec["fields"]:
        locate = f["locate"]
        extract = f["extract"]
        for pg in pages:
            page_no = pg.get("page")
            text = pg.get("text") or ""
            doc_id = pg.get("doc_id") or spec.get("target") or ""
            for unit in _iter_units(locate, text):
                m = unit["m"]
                if extract["kind"] == "text":
                    qtext = unit["text"]
                    start, end = unit["start"], unit["end"]
                else:  # group
                    gi = extract["index"]
                    if m is None or gi > (m.lastindex or 0):
                        continue
                    # 组偏移相对段落；_paragraphs 已把段落起点算进 unit["start"] 的口径里
                    para_start = unit["start"] - m.start()
                    qtext = m.group(gi)
                    start, end = para_start + m.start(gi), para_start + m.end(gi)

                bound: dict = {}
                if unit["row_label"] is not None:
                    bound["row_label"] = unit["row_label"]
                if unit_hint:
                    bound["unit"] = unit_hint
                if period_hint:
                    bound["period"] = period_hint

                spans.append(
                    Span(
                        span_id=f"S-{idx:06d}",
                        doc_id=doc_id,
                        page=page_no,
                        char_range=(start, end),
                        quoted_text=qtext,
                        decimals_as_disclosed=decimals_of(qtext),
                        span_kind=SpanKind.TABLE_ROW.value if locate["kind"] == "table_row"
                        else SpanKind.PARAGRAPH.value,
                        bound_context=bound,
                        extractor=ExtractorTier.SPEC.value,
                        spec_id=spec.get("spec_id", ""),
                    )
                )
                idx += 1
    return spans


def load_spec(path: str | Path) -> dict:
    """读一份规格 JSON（UTF-8）。"""
    return json.loads(Path(path).read_text(encoding="utf-8"))


__all__ = ["validate_spec", "run_spec", "load_spec", "FIELD_WHITELIST",
           "LOCATE_KINDS", "EXTRACT_KINDS"]
