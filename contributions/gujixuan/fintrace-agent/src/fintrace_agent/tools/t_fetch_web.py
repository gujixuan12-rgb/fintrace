"""t_fetch_web —— 抓取网页原文快照（**预留接口**，BUILD_SPEC §7）。

``reserved=True``：未配置时 ``execute`` 直接给 ``not_configured``，不触网。
配置方式：``ctx.config["web"]`` 里放 ``{"enabled": True, "fetch": <callable>}``，
由上层注入真实抓取实现。抓到的大正文由 registry 统一落 blobs。
"""
from __future__ import annotations

from .registry import ToolError, ToolOutcome, ToolSpec, ctx_config, register

SPEC = ToolSpec(
    name="t_fetch_web",
    params={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "要抓取的网页地址"},
        },
        "required": ["url"],
    },
    receipt={"fields": ["snapshot"]},
    preconditions=["在 ctx.config['web'] 里配置抓取后端（enabled + fetch 回调）"],
    timeout_s=20,
    enabled=True,
    reserved=True,
)


def _configured(ctx) -> bool:
    web = ctx_config(ctx).get("web") or {}
    return bool(web.get("enabled")) and callable(web.get("fetch"))


def run(args: dict, ctx) -> ToolOutcome:
    web = ctx_config(ctx).get("web") or {}
    fn = web.get("fetch")
    if not callable(fn):
        raise ToolError("not_configured", "网页抓取后端未接入")
    url = str(args.get("url", "")).strip()
    if not url:
        raise ToolError("bad_args", "缺少 url")
    snapshot = fn({"url": url})
    return ToolOutcome(result={"snapshot": snapshot})


register(SPEC, run, _configured)

__all__ = ["SPEC", "run"]
