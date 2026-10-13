"""t_knowledge —— 知识库只读查询（BUILD_SPEC §7）。

把 ``KnowledgeBase``（规则层）的权威查询包成工具：概念归一、公式、关系规则、
三元组一致性、单位精度、全部指标。知识库对象由 ``ctx.kb`` 注入。
"""
from __future__ import annotations

from .registry import ToolError, ToolOutcome, ToolSpec, ctx_get, register

OPS = ("resolve_metric", "formula", "relation_rule", "context_check", "precision_for", "all_metrics")

SPEC = ToolSpec(
    name="t_knowledge",
    params={
        "type": "object",
        "properties": {
            "op": {"type": "string", "enum": list(OPS)},
            "name": {"type": "string", "description": "resolve_metric 的说法/别名"},
            "anchor": {"type": "integer", "description": "resolve_metric 的数值位置（可选）"},
            "metric_id": {"type": "string"},
            "caliber": {"type": "string"},
            "rule_id": {"type": "string"},
            "entity": {"type": "string"},
            "period": {"type": "string"},
            "unit": {"type": "string"},
        },
        "required": ["op"],
    },
    receipt={"fields": ["op", "hit", "value"]},
    preconditions=["ctx.kb 已注入知识库"],
    timeout_s=10,
)


def run(args: dict, ctx) -> ToolOutcome:
    op = str(args.get("op", "")).strip()
    if op not in OPS:
        raise ToolError("bad_args", f"未知 op：{op!r}（可选 {list(OPS)}）")
    kb = ctx_get(ctx, "kb")
    if kb is None:
        raise ToolError("not_configured", "未提供知识库（ctx.kb）")

    if op == "resolve_metric":
        name = str(args.get("name", "")).strip()
        if not name:
            raise ToolError("bad_args", "缺少 name")
        anchor = args.get("anchor")
        value = kb.resolve_metric(name, anchor if isinstance(anchor, int) and not isinstance(anchor, bool) else None)
    elif op == "formula":
        metric_id = str(args.get("metric_id", "")).strip()
        if not metric_id:
            raise ToolError("bad_args", "缺少 metric_id")
        value = kb.formula(metric_id, str(args.get("caliber", "")))
    elif op == "relation_rule":
        rule_id = str(args.get("rule_id", "")).strip()
        if not rule_id:
            raise ToolError("bad_args", "缺少 rule_id")
        value = kb.relation_rule(rule_id)
    elif op == "context_check":
        value = kb.context_check(str(args.get("entity", "")), str(args.get("period", "")),
                                 str(args.get("unit", "")))
    elif op == "precision_for":
        unit = str(args.get("unit", "")).strip()
        if not unit:
            raise ToolError("bad_args", "缺少 unit")
        value = kb.precision_for(str(args.get("metric_id", "")), unit)
    else:  # all_metrics
        value = kb.all_metrics()

    hit = value is not None and not (isinstance(value, list) and not value)
    return ToolOutcome(result={"op": op, "hit": hit, "value": value})


register(SPEC, run)

__all__ = ["SPEC", "run", "OPS"]
