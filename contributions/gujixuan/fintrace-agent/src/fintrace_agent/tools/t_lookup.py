"""t_lookup —— 查公司事实库（实例层，BUILD_SPEC §7）。

``only_facts=True``（默认）只返回双轴都过的条目（attested + bound）；
``only_facts=False`` 连同候选/未绑定条目一并返回（留痕可见）。事实库由 ``ctx.facts`` 注入。
"""
from __future__ import annotations

from .registry import ToolError, ToolOutcome, ToolSpec, ctx_get, register

SPEC = ToolSpec(
    name="t_lookup",
    params={
        "type": "object",
        "properties": {
            "subject": {"type": "string"},
            "period": {"type": "string"},
            "metric": {"type": "string"},
            "kind": {"type": "string"},
            "only_facts": {"type": "boolean", "description": "默认 true：只取双轴都过的"},
            "assertion_id": {"type": "string", "description": "给 id 则点查一条"},
        },
        "required": [],
    },
    receipt={"fields": ["assertions", "count", "only_facts"]},
    preconditions=["ctx.facts 已注入事实库", "至少给一个筛选条件或 assertion_id"],
    timeout_s=10,
)


def run(args: dict, ctx) -> ToolOutcome:
    facts = ctx_get(ctx, "facts")
    if facts is None:
        raise ToolError("not_configured", "未提供事实库（ctx.facts）")

    assertion_id = str(args.get("assertion_id", "")).strip()
    if assertion_id:
        item = facts.get_assertion(assertion_id)
        return ToolOutcome(result={
            "assertions": [item.to_dict()] if item else [],
            "count": 1 if item else 0,
            "only_facts": None,
        })

    subject = str(args.get("subject", "")).strip()
    period = str(args.get("period", "")).strip()
    metric = str(args.get("metric", "")).strip()
    kind = str(args.get("kind", "")).strip()
    if not (subject or period or metric or kind):
        raise ToolError("bad_args", "至少给出 subject/period/metric/kind 之一，或 assertion_id")

    only_facts = bool(args.get("only_facts", True))
    rows = facts.query(subject=subject, period=period, metric=metric, kind=kind,
                       only_facts=only_facts)
    return ToolOutcome(result={
        "assertions": [a.to_dict() for a in rows],
        "count": len(rows),
        "only_facts": only_facts,
    })


register(SPEC, run)

__all__ = ["SPEC", "run"]
