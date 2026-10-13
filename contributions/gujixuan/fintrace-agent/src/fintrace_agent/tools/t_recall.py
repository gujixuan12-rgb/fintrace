"""t_recall —— 取回原文（Span 级，BUILD_SPEC §7）。

从证据日志（``ctx.evidence``）按关键词 / 页码区间 / span_id 取回**原文**（不妥协成摘要）。
至少要给一种筛选条件，否则 ``bad_args``。
"""
from __future__ import annotations

from .registry import ToolError, ToolOutcome, ToolSpec, ctx_get, register

SPEC = ToolSpec(
    name="t_recall",
    params={
        "type": "object",
        "properties": {
            "keywords": {"type": "array", "items": {"type": "string"},
                         "description": "全部命中才算（忽略空白差异）"},
            "span_ids": {"type": "array", "items": {"type": "string"}},
            "page_from": {"type": "integer"},
            "page_to": {"type": "integer"},
            "limit": {"type": "integer", "description": "默认 50"},
        },
        "required": [],
    },
    receipt={"fields": ["spans", "count"]},
    preconditions=["ctx.evidence 已注入证据日志", "至少给 keywords / span_ids / 页码区间之一"],
    timeout_s=10,
)


def run(args: dict, ctx) -> ToolOutcome:
    ev = ctx_get(ctx, "evidence")
    if ev is None:
        raise ToolError("not_configured", "未提供证据日志（ctx.evidence）")

    span_ids = args.get("span_ids") or []
    keywords = args.get("keywords") or []
    if isinstance(span_ids, str):
        span_ids = [span_ids]
    if isinstance(keywords, str):
        keywords = [keywords]
    page_from = args.get("page_from")
    page_to = args.get("page_to")

    if not span_ids and not keywords and page_from is None and page_to is None:
        raise ToolError("bad_args", "至少给出 keywords / span_ids / page 范围之一")

    if span_ids:
        hits = [s for s in (ev.span(str(x)) for x in span_ids) if s is not None]
    else:
        kws = ["".join(str(k).split()) for k in keywords if str(k).strip()]
        hits = []
        for span in ev.spans():
            if page_from is not None and (span.page is None or span.page < int(page_from)):
                continue
            if page_to is not None and (span.page is None or span.page > int(page_to)):
                continue
            flat = "".join(span.quoted_text.split())
            if kws and not all(k in flat for k in kws):
                continue
            hits.append(span)

    try:
        limit = int(args.get("limit", 50))
    except (TypeError, ValueError):
        raise ToolError("bad_args", "limit 必须是整数")
    if limit < 0:
        raise ToolError("bad_args", "limit 不能为负")
    hits = hits[:limit]
    return ToolOutcome(result={"spans": [s.to_dict() for s in hits], "count": len(hits)})


register(SPEC, run)

__all__ = ["SPEC", "run"]
