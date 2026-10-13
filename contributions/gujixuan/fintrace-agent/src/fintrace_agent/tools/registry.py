"""工具注册表与统一执行入口（BUILD_SPEC 第 7 节）。

纪律：

* ``execute`` 是回路调用工具的唯一入口。未注册 → ``unknown_tool``；未启用 →
  ``disabled``；预留但未配置 → ``not_configured``。这三种都在**调用处理器之前**判定，
  所以预留联网工具在未配置时绝不会发起网络请求。
* 工具抛出的任何异常都在这里被转成 ``ok=False`` 的收据，绝不把异常抛回回路。
* 计时 ``ms``；大结果（超长字符串 / 超长列表）落内容寻址库，收据里只留 ``blob_refs``。

工具模块自己在导入时调用 :func:`register` 注册；本模块在导入时自动加载全部 ``t_*.py``。
只用标准库。
"""
from __future__ import annotations

import importlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..contracts import ProgressKind, ToolReceipt

#: 超过这个长度的字符串 / 超过这个长度的列表算「大结果」，落 blobs
BIG_TEXT_LIMIT = 4096
BIG_LIST_LIMIT = 200

#: 需要自动加载的工具模块（顺序无关，模块自身负责注册）
_TOOL_MODULES: tuple[str, ...] = (
    "t_search_official",
    "t_search_web",
    "t_fetch_web",
    "t_parse",
    "t_extract",
    "t_knowledge",
    "t_compute",
    "t_review",
    "t_lookup",
    "t_recall",
    "t_spec_extract",
    "t_author_tool",
)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class ToolSpec:
    """一件工具的静态说明（契约字段与 BUILD_SPEC 第 7 节一致）。"""

    name: str
    params: dict                      # JSONSchema 片段
    receipt: dict                     # 结果收据里会出现哪些字段
    preconditions: list               # 人类可读；判定逻辑放 agent/tool_guard
    timeout_s: int = 20
    enabled: bool = True
    reserved: bool = False            # True = 预留接口，未配置时明确失败


@dataclass
class ToolOutcome:
    """工具处理器的返回值：结果 + 收据元信息（进展 / 新证据 / 否决项）。"""

    result: dict = field(default_factory=dict)
    blob_refs: list = field(default_factory=list)
    new_evidence: list = field(default_factory=list)
    rejected: list = field(default_factory=list)
    progress: str = ProgressKind.NONE.value


class ToolError(Exception):
    """可预期的工具失败。``code`` 进收据的 ``error`` 字段。"""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message


@dataclass
class ToolContext:
    """回路传给工具的运行时上下文（工具只读它，不写）。

    ``config`` 是自由字典：知识库 / 事实库 / 证据日志各自挂一个具体对象，
    联网与模型相关开关也放这里（例如 ``web``、``llm_cfg``、``sourcing_cache``）。
    """

    run_id: str = ""
    kb: Any = None
    facts: Any = None
    evidence: Any = None
    config: dict = field(default_factory=dict)


def ctx_get(ctx: Any, key: str, default: Any = None) -> Any:
    """从 ToolContext / dict / 任意对象里取一个字段。"""
    if ctx is None:
        return default
    if isinstance(ctx, dict):
        return ctx.get(key, default)
    return getattr(ctx, key, default)


def ctx_config(ctx: Any) -> dict:
    """取上下文里的 ``config`` 字典；取不到就返回空字典。"""
    cfg = ctx_get(ctx, "config", {})
    return cfg if isinstance(cfg, dict) else {}


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, dict] = {}


def register(spec: ToolSpec, run: Optional[Callable] = None,
             configured: Optional[Callable] = None) -> None:
    """注册一件工具。``register(spec)`` / ``register(spec, run, configured)`` 都合法。

    ``configured(ctx) -> bool`` 只对 ``reserved=True`` 的工具生效：返回 False 时
    ``execute`` 直接给 ``not_configured``，不调用 ``run``。
    """
    if not isinstance(spec, ToolSpec):
        raise TypeError("register 只接受 ToolSpec")
    _REGISTRY[spec.name] = {"spec": spec, "run": run, "configured": configured}


def unregister(name: str) -> None:
    """移除一件工具（主要给测试清理用）。"""
    _REGISTRY.pop(name, None)


def reset() -> None:
    """清空注册表（主要给测试用）。"""
    _REGISTRY.clear()


def get(name: str) -> Optional[ToolSpec]:
    """按名字取工具说明；没有返回 ``None``。"""
    entry = _REGISTRY.get(name or "")
    return entry["spec"] if entry else None


def all_specs() -> list[ToolSpec]:
    """全部已注册工具，按名字排序（稳定输出）。"""
    return [_REGISTRY[k]["spec"] for k in sorted(_REGISTRY)]


def configured_map(ctx: Any = None) -> dict:
    """``{工具名: 是否已配置}`` —— 只覆盖 ``reserved=True`` 的工具。

    给决策上下文的「工具手册」用：未配置的预留工具必须让模型看见，否则它会
    反复去选一个注定返回 ``not_configured`` 的动作，白烧步数（真实跑过一轮才发现）。
    """
    out: dict[str, bool] = {}
    for name, entry in _REGISTRY.items():
        spec: ToolSpec = entry["spec"]
        if not spec.reserved:
            continue
        fn = entry.get("configured")
        try:
            out[name] = bool(fn(ctx)) if callable(fn) else False
        except Exception:                                        # noqa: BLE001
            out[name] = False
    return out


def register_all(force: bool = False) -> None:
    """导入全部 ``t_*.py``，触发它们各自的注册（幂等）。"""
    global _LOADED
    if _LOADED and not force:
        return
    pkg = __package__ or "fintrace_agent.tools"
    for mod in _TOOL_MODULES:
        importlib.import_module(f"{pkg}.{mod}")
    _LOADED = True


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------


def _ms(t0: float) -> int:
    return int(round((time.perf_counter() - t0) * 1000))


def _receipt(name: str, ok: bool, *, error: str = "", result: Optional[dict] = None,
             ms: int = 0, blob_refs: Optional[list] = None) -> ToolReceipt:
    return ToolReceipt(
        tool=name,
        ok=ok,
        result=dict(result or {}),
        error=error,
        ms=ms,
        blob_refs=list(blob_refs or []),
    )


def _offload_blobs(result: dict) -> tuple[dict, list]:
    """把大结果落内容寻址库，返回（替换后的结果, blob_refs）。

    只对**顶层**超长字符串 / 超长列表生效，保证结果字典的形状不变；
    被替换的值写成一个 ``{"blob_ref", "chars", "type"}`` 说明。
    """
    from ..store import blobs

    refs: list[str] = []
    out: dict = {}
    for key, value in result.items():
        is_big = (
            isinstance(value, str) and len(value) > BIG_TEXT_LIMIT
        ) or (
            isinstance(value, list) and len(value) > BIG_LIST_LIMIT
        )
        if not is_big:
            out[key] = value
            continue
        payload = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        sha = blobs.put_text(payload)
        refs.append(sha)
        out[key] = {
            "blob_ref": sha,
            "chars": len(payload),
            "type": "text" if isinstance(value, str) else "json",
        }
    return out, refs


def execute(name: str, args: Optional[dict], ctx: Any = None) -> ToolReceipt:
    """执行一件工具，永远返回 ``ToolReceipt``（不抛异常）。"""
    t0 = time.perf_counter()
    entry = _REGISTRY.get(name or "")
    if entry is None:
        return _receipt(name, False, error="unknown_tool", ms=_ms(t0))

    spec: ToolSpec = entry["spec"]
    if not spec.enabled:
        return _receipt(name, False, error="disabled", ms=_ms(t0))

    if spec.reserved:
        cfg_fn = entry.get("configured")
        configured = bool(cfg_fn(ctx)) if callable(cfg_fn) else False
        if not configured:
            return _receipt(name, False, error="not_configured", ms=_ms(t0))

    run = entry.get("run")
    if run is None:
        return _receipt(name, False, error="no_handler", ms=_ms(t0))

    if args is None:
        args = {}
    if not isinstance(args, dict):
        return _receipt(name, False, error="bad_args",
                        result={"message": "args 必须是对象"}, ms=_ms(t0))

    try:
        out = run(args, ctx)
    except ToolError as exc:
        return _receipt(name, False, error=exc.code,
                        result=({"message": exc.message} if exc.message else {}),
                        ms=_ms(t0))
    except Exception as exc:  # noqa: BLE001 —— 一律兜住，绝不抛回回路
        return _receipt(name, False, error="tool_exception",
                        result={"message": f"{type(exc).__name__}: {exc}"},
                        ms=_ms(t0))

    if isinstance(out, ToolOutcome):
        outcome = out
    elif isinstance(out, dict):
        outcome = ToolOutcome(result=out)
    else:
        return _receipt(name, False, error="bad_return",
                        result={"message": f"处理器返回了 {type(out).__name__}"},
                        ms=_ms(t0))

    result, extra_refs = _offload_blobs(dict(outcome.result))
    blob_refs = list(outcome.blob_refs) + extra_refs
    return ToolReceipt(
        tool=name,
        ok=True,
        result=result,
        ms=_ms(t0),
        blob_refs=blob_refs,
        new_evidence=list(outcome.new_evidence),
        rejected=list(outcome.rejected),
        progress=outcome.progress,
    )


# 导入即完成注册（各 t_*.py 在自身模块底部调用 register）。
_LOADED = False
register_all()


__all__ = [
    "ToolSpec",
    "ToolOutcome",
    "ToolError",
    "ToolContext",
    "register",
    "unregister",
    "reset",
    "get",
    "all_specs",
    "register_all",
    "configured_map",
    "execute",
    "ctx_get",
    "ctx_config",
    "BIG_TEXT_LIMIT",
    "BIG_LIST_LIMIT",
]
