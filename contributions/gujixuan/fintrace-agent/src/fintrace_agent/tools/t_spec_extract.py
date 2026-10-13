"""t_spec_extract —— 声明式抽取（BUILD_SPEC §7）。

LLM 不写代码，只写规格；本工具先 ``parsers.spec_extract.validate_spec``，
**校验有错一律拒绝执行**（``invalid_spec``，不交给解释器）。
"""
from __future__ import annotations

from ..parsers import run_spec, validate_spec
from .registry import ToolError, ToolOutcome, ToolSpec, register

SPEC = ToolSpec(
    name="t_spec_extract",
    params={
        "type": "object",
        "properties": {
            "spec": {"type": "object", "description": "抽取规格（须过 validate_spec）"},
            "pages": {"type": "array", "description": "逐页文本 [{'page','text'}]"},
        },
        "required": ["spec", "pages"],
    },
    receipt={"fields": ["spans", "count", "spec_id"]},
    preconditions=["spec 必须通过 parsers.spec_extract.validate_spec", "pages 非空"],
    timeout_s=20,
)


def run(args: dict, ctx) -> ToolOutcome:
    spec = args.get("spec")
    pages = args.get("pages")
    if not isinstance(spec, dict):
        raise ToolError("bad_args", "缺少 spec 对象")
    if not isinstance(pages, list) or not pages:
        raise ToolError("bad_args", "缺少 pages 逐页文本")

    errors = validate_spec(spec)
    if errors:
        raise ToolError("invalid_spec", "；".join(errors))

    spans = run_spec(spec, pages)
    return ToolOutcome(result={
        "spans": [s.to_dict() for s in spans],
        "count": len(spans),
        "spec_id": spec.get("spec_id", ""),
    })


register(SPEC, run)

__all__ = ["SPEC", "run"]
