"""t_review —— 论证审查（BUILD_SPEC §7）。

默认 ``enabled=False``：未启用时 ``execute`` 直接给 ``disabled``，处理器不会被调用。
启用需上层显式改注册表（本模块不提供隐式开关）。
"""
from __future__ import annotations

from .registry import ToolError, ToolOutcome, ToolSpec, register

SPEC = ToolSpec(
    name="t_review",
    params={
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "待审查的对象（findings / 报告）"},
            "criteria": {"type": "array", "items": {"type": "string"}},
        },
        "required": [],
    },
    receipt={"fields": ["review"]},
    preconditions=["本工具默认关闭，需先启用"],
    timeout_s=20,
    enabled=False,
)


def run(args: dict, ctx) -> ToolOutcome:  # pragma: no cover - 未启用不会执行
    raise ToolError("disabled", "t_review 默认关闭；启用前不得执行")


register(SPEC, run)

__all__ = ["SPEC", "run"]
