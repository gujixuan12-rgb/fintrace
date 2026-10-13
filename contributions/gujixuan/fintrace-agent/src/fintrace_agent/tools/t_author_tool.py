"""t_author_tool —— 运行时工具自举（BUILD_SPEC §7）。

**不启用**（``enabled=False``，与 ``contracts.ACTIONS_DISABLED_BY_DEFAULT`` 一致）：
`execute` 在调用处理器之前就返回 ``disabled``，绝不放行。

启用前置条件见 v2.2 §5.4：必须先在 **OS 级隔离**（独立进程 / 容器 + 只读挂载 + 网络与文件
系统白名单）里运行自举产物，审过再注册；在无隔离的核心回路里永不放开本工具。
"""
from __future__ import annotations

from .registry import ToolError, ToolOutcome, ToolSpec, register

SPEC = ToolSpec(
    name="t_author_tool",
    params={
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "spec": {"type": "object"},
        },
        "required": [],
    },
    receipt={"fields": []},
    preconditions=["此动作不启用"],
    timeout_s=5,
    enabled=False,
)


def run(args: dict, ctx) -> ToolOutcome:  # pragma: no cover - 未启用不会执行
    raise ToolError("disabled", "t_author_tool 不启用（BUILD_SPEC §7）")


register(SPEC, run)

__all__ = ["SPEC", "run"]
