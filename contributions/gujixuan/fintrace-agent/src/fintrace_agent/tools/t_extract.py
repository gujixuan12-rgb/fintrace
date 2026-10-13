"""t_extract —— 材料正文 → 可核验断言（BUILD_SPEC §7）。

首选大模型（``fintrace_engine.llm.decompose_claims``）；**无 key 或调用失败一律回落规则拆解**，
并在 ``extracted_by.how`` 里如实标注 ``llm`` / ``rule``。规则拆解只认「数字+单位」，忠于原文。

离线保证：``allow_llm=False``（或 ``ctx.config['llm']['enabled']`` 为假、或没有可用 key）时
**绝不发起任何模型调用**。
"""
from __future__ import annotations

import re

from .registry import ToolError, ToolOutcome, ToolSpec, ctx_config, ctx_get, register

PROMPT_VERSION = "p1"

SPEC = ToolSpec(
    name="t_extract",
    params={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "材料正文"},
            "pages": {"type": "array", "description": "或逐页文本 [{'page','text'}]"},
            "doc_id": {"type": "string",
                       "description": "只给 doc_id 时，从已登记的证据里拼回该材料正文"},
            "source": {"type": "string", "description": "材料来源说明"},
            "page": {"type": "integer", "description": "整体页码（拆不出细粒度时用）"},
            "allow_llm": {"type": "boolean", "description": "是否允许调用大模型（默认按配置）"},
        },
        "required": [],
    },
    receipt={"fields": ["claims", "count", "doc_id", "extracted_by"]},
    preconditions=["text / pages / doc_id 至少给一个；给 doc_id 时需 ctx.evidence 已注入"],
    timeout_s=30,
)

#: 数字 + 紧跟单位（长单位排在前，避免「万元」被「元」截胡）
_NUM_UNIT = re.compile(
    r"(-?\d[\d,]*(?:\.\d+)?)\s*(个百分点|元/股|元/10股|百万元|万元|亿元|千元|万股|亿股|元|%|股|倍)"
)
#: 「科目（单位） 期间 数值」形态 —— 年报「主要会计数据」表的常见版式，
#: 单位在数值**前面**，`_NUM_UNIT` 抓不到。缺了这条，整页表格会一条都抽不出来。
_ROW_LABEL_UNIT = re.compile(
    r"([\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z（）()·]{1,28}?)\s*[（(]\s*"
    r"(元/10股|元/股|百万元|万元|亿元|千元|万股|亿股|元|%|股|倍)\s*[）)]"
)
#: 任意数值
_NUM_ANY = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
#: 期间提示（表格里的列头）：2023年 / 2023年末 / 本报告期 等
_PERIOD_TOKEN = re.compile(r"(\d{4}\s*年(?:度|末|初)?|本报告期|报告期|本期|上期)")
#: 纯 4 位年份（1900~2100）——不是金额，要跳过
_YEAR_LIKE = re.compile(r"^(?:19|20|21)\d{2}$")


def _amount_after(rest: str) -> str:
    """取「单位之后」的那个金额：跳过年份（``2023`` / ``2023年``），取第一个真正的数值。

    表格行是 ``科目（元） 2023年 1,243,188,170.21``：单位后面先出现的是年份，
    直接取第一个数字会把年份当成金额（实测抽出来 6 个 ``value=2023``）。
    """
    for t in _NUM_ANY.finditer(rest or ""):
        raw = t.group(0)
        whole = raw.replace(",", "")
        if "." not in whole and _YEAR_LIKE.match(whole):
            continue
        return raw
    return ""
#: 句子切分
_SENT_SPLIT = re.compile(r"[。；！？\n]+")


def _norm_claim(claim: dict, default_page) -> dict:
    return {
        "claim_text": str(claim.get("claim_text", "")).strip(),
        "metric": str(claim.get("metric", "") or ""),
        "value": str(claim.get("value", "") or ""),
        "unit": str(claim.get("unit", "") or ""),
        "period": str(claim.get("period", "") or ""),
        "subject": str(claim.get("subject", "") or ""),
        "page": claim.get("page", default_page),
        "quoted_text": str(claim.get("quoted_text", "") or ""),
        "locatable": bool(claim.get("locatable", True)),
        "reason": str(claim.get("reason", "") or ""),
    }


#: PDF 文本会把长数字在行末切断：``2,177,194,04\n9.11``。只在这种「换行两侧都是数字」时拼回；
#: 不能把任意空白都吃掉（``2023年 1,243`` 那样会拼成 ``20231,243``）。
#: 断行形态 1：千分位分组没写完 —— ``1,243,188,17`` + ``0.21`` / ``2,177,194,04`` + ``9.11``
_WRAP_MID_GROUP = re.compile(
    r"(\d{1,3}(?:,\d{3})*,\d{1,2})\s*\n\s*(\d{1,3}(?:,\d{3})*(?:\.\d+)?)"
)
#: 断行形态 2：小数位被切断 —— ``23,021,806.6`` + ``9``（右侧必须是 1~2 位且后面不再接数字，
#: 否则会把下一列的另一个数字粘上来）
_WRAP_MID_DECIMAL = re.compile(r"(\d[\d,]*\.\d{1,2})\s*\n\s*(\d{1,2})(?![,\d])")
#: 断行形态 3：小数点被甩到下一行 —— ``286,681,291`` + ``.13``
_WRAP_DOT_NEXT_LINE = re.compile(r"(\d[\d,]*)\s*\n\s*(\.\d{1,4})(?![\d])")
#: 标签跨行：「归属于上市公\n司股东的净利\n润（元）」——中文之间的换行要吃掉，
#: 否则标签只剩「的净利润」，知识库认不出来（实测整页表格因此一条都登记不了）。
_CJK_LINEBREAK = re.compile(r"(?<=[\u4e00-\u9fa5）)])\s*\n\s*(?=[\u4e00-\u9fa5（])")


def _join_wrapped_numbers(text: str) -> str:
    """PDF 文本清理：拼回被换行切断的数字、吃掉中文之间的换行。

    必须在**切句之前**做（切句本身就在换行处断句）。两条断行规则都带上守卫，
    不是见换行就拼 —— 否则 ``1,243,188,170.21`` 与下一行的 ``2023`` 会被粘成一个怪数。
    """
    s = text or ""
    s = _WRAP_MID_GROUP.sub(lambda m: m.group(1) + m.group(2), s)
    s = _WRAP_MID_DECIMAL.sub(lambda m: m.group(1) + m.group(2), s)
    s = _WRAP_DOT_NEXT_LINE.sub(lambda m: m.group(1) + m.group(2), s)
    return _CJK_LINEBREAK.sub("", s)


def _resolve_name(kb, text: str, anchor) -> str:
    """把文本里的说法归一到知识库的指标名（失败返回空串，不影响拆解）。"""
    if kb is None:
        return ""
    try:
        concept = kb.resolve_metric(text, anchor)
        if concept:
            return str(concept.get("cn", "") or concept.get("metric_id", "") or "")
    except Exception:  # noqa: BLE001
        pass
    return ""


def _rule_decompose(text: str, *, doc_id: str, page, kb) -> list[dict]:
    """规则拆解：按句切分，抓数值，指标名尽力归一到知识库。

    先做两件清理：拼回被换行切断的数字（``2,177,194,04\n9.11``），
    否则抽出来的 raw_text 不是纯数字串，W6 会判「无法判定披露精度」。
    然后两条版式都认（缺一条就会整表抽空）：
    1. 「数字 + 单位」，如 ``12.43 亿元``；
    2. 「科目（单位） 期间 数值」，如 ``营业收入（元） 2023年 1,243,188,170.21``
       —— 年报「主要会计数据」表就是这种，单位在数值前面。
    """
    text = _join_wrapped_numbers(text)
    claims: list[dict] = []

    # 版式 2（表格行）**在整页文本上做**，不按句子切：
    # 「总资产（元） \n2,177,194,049.11」里标签与数值中间就有换行，
    # 按句切会把它们分到两段，结果整表抽空（实测就是这么空的）。
    seen_rows: set = set()
    for rm in _ROW_LABEL_UNIT.finditer(text):
        label = rm.group(1).strip()
        tail = text[rm.end():rm.end() + 60]           # 只看紧跟其后的 60 字符
        value = _amount_after(tail)
        if not value:
            continue
        m_period = _PERIOD_TOKEN.search(tail[:tail.index(value) + len(value)])
        key = (label, value, rm.group(2))
        if key in seen_rows:
            continue
        seen_rows.add(key)
        end_at = rm.end() + tail.index(value) + len(value)
        row_text = text[rm.start():end_at].strip()
        claims.append({
            "claim_text": row_text,
            "metric": _resolve_name(kb, label, None) or _resolve_name(kb, row_text, 0),
            "value": value,
            "unit": rm.group(2),
            "period": (m_period.group(0).replace(" ", "") if m_period else ""),
            "period_hint": (m_period.group(0).replace(" ", "") if m_period else ""),
            "subject": "",
            "page": page,
            "quoted_text": row_text,
            "locatable": page is not None,
            "reason": "规则拆解（表格行：科目（单位） 期间 数值）",
        })

    for sentence in _SENT_SPLIT.split(text):
        s = sentence.strip()
        if not s:
            continue

        m = _NUM_UNIT.search(s)                       # 版式 1：数字 + 单位
        if not m:
            continue
        metric = _resolve_name(kb, s, m.start())
        claims.append({
            "claim_text": s,
            "metric": metric,
            "value": m.group(1),
            "unit": m.group(2),
            "period": "",
            "subject": "",
            "page": page,
            "quoted_text": s,
            "locatable": page is not None,
            "reason": "规则拆解（未调用大模型）",
        })
    return claims


def _wanted_pages(pages) -> set:
    """``pages`` 里可能混着整数页码和 ``{'page','text'}`` 字典；只取整数页码。"""
    want: set = set()
    for p in (pages or []):
        if isinstance(p, dict):
            continue
        try:
            want.add(int(p))
        except (TypeError, ValueError):
            continue
    return want


def _text_from_evidence(ev, doc_id: str, pages) -> tuple[str, object]:
    """从证据日志把某份材料**已登记**的原文片段拼回正文。

    为什么需要：Agent 解析完材料之后，最自然的下一步是「从这份材料里抽断言」，
    而它手里只有 doc_id —— 强制它自己搬 text 既费上下文又搬不动（正文早被
    内容寻址卸载成 blob 了）。所以这里按 doc_id（可选页码）从证据库拼回来。
    """
    if ev is None:
        return "", None
    rows = [s for s in ev.spans() if str(getattr(s, "doc_id", "") or "") == doc_id]
    want = _wanted_pages(pages)
    if want:
        rows = [s for s in rows if getattr(s, "page", None) in want]
    text = "\n".join(str(getattr(s, "quoted_text", "") or "") for s in rows)
    first_page = getattr(rows[0], "page", None) if len(rows) == 1 else None
    return text, first_page


def run(args: dict, ctx) -> ToolOutcome:
    doc_id = str(args.get("doc_id", ""))
    text = args.get("text")
    pages = args.get("pages") or []
    if not isinstance(text, str) or not text.strip():
        if isinstance(pages, list):
            text = "\n".join(str((p or {}).get("text", ""))
                             for p in pages if isinstance(p, dict))

    page = args.get("page")
    if (not isinstance(text, str) or not text.strip()) and doc_id:
        ev = ctx_get(ctx, "evidence")
        if ev is None:
            raise ToolError("not_configured",
                            "只给了 doc_id，但未注入证据日志（ctx.evidence），无法拼回正文")
        text, inferred = _text_from_evidence(ev, doc_id, pages)
        if page is None:
            page = inferred

    if not isinstance(text, str) or not text.strip():
        raise ToolError("bad_args",
                        "没有可拆解的材料正文：请给 text、或 pages（逐页文本）、"
                        "或 doc_id（从已登记的证据里拼回）")

    source = str(args.get("source", ""))
    cfg = ctx_config(ctx)
    llm_cfg = cfg.get("llm_cfg")

    allow = args.get("allow_llm")
    use_llm = True if allow is None else bool(allow)
    if use_llm:
        use_llm = bool((cfg.get("llm") or {}).get("enabled", True))

    how, model, reason = "rule", "", ""
    claims: list[dict] = []
    if use_llm:
        try:
            from fintrace_engine import llm as _llm
            if _llm.available(llm_cfg):
                raw = _llm.decompose_claims(text, source=source, cfg=llm_cfg)
                claims = [_norm_claim(c, page) for c in raw if isinstance(c, dict)]
                how = "llm"
                model = str((llm_cfg or _llm.load_config()).get("model", ""))
            else:
                reason = "未配置大模型 API Key"
        except Exception as exc:  # noqa: BLE001 —— 失败即回落规则
            reason = f"{type(exc).__name__}: {exc}"

    if how != "llm":
        claims = _rule_decompose(text, doc_id=doc_id, page=page, kb=ctx_get(ctx, "kb"))
        how = "rule"

    return ToolOutcome(result={
        "claims": claims,
        "count": len(claims),
        "doc_id": doc_id,
        "extracted_by": {
            "how": how,
            "model": model,
            "prompt_version": PROMPT_VERSION,
            "reason": reason,
        },
    })


register(SPEC, run)

__all__ = ["SPEC", "run"]
