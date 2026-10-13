"""t_search_web —— 联网搜索线索（**预留接口**，BUILD_SPEC §7）。

``reserved=True``：未配置时 ``execute`` 直接给 ``not_configured``，**不调用处理器、不触网**。
配置方式是在 ``ctx.config["web"]`` 里放 ``{"enabled": True, "search": <callable>}``；
真正的搜索实现由上层（网关 / 插件）注入，本模块不内建任何网络客户端。
"""
from __future__ import annotations

from .registry import ToolError, ToolOutcome, ToolSpec, ctx_config, register

SPEC = ToolSpec(
    name="t_search_web",
    params={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索词"},
            "limit": {"type": "integer", "description": "结果条数上限，默认 5"},
        },
        "required": ["query"],
    },
    receipt={"fields": ["leads"]},
    preconditions=["在 ctx.config['web'] 里配置搜索后端（enabled + search 回调）"],
    timeout_s=20,
    enabled=True,
    reserved=True,
)


def _configured(ctx) -> bool:
    web = ctx_config(ctx).get("web") or {}
    return bool(web.get("enabled")) and callable(web.get("search"))


def run(args: dict, ctx) -> ToolOutcome:
    web = ctx_config(ctx).get("web") or {}
    fn = web.get("search")
    if not callable(fn):
        # 正常不会走到这里（execute 已拦 not_configured），兜底仍不触网。
        raise ToolError("not_configured", "联网搜索后端未接入")
    query = str(args.get("query", "")).strip()
    if not query:
        raise ToolError("bad_args", "缺少 query")
    try:
        limit = int(args.get("limit", 5))
    except (TypeError, ValueError):
        raise ToolError("bad_args", "limit 必须是整数")
    leads = fn({"query": query, "limit": limit})
    return ToolOutcome(result={"leads": list(leads or [])})


register(SPEC, run, _configured)

__all__ = ["SPEC", "run"]
