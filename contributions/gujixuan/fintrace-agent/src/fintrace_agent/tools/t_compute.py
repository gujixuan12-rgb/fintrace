"""t_compute —— 确定性复算（BUILD_SPEC §7）。

纪律：**全用 Decimal 复算，模型不参与**。收据里必须留全舍入痕迹：

* ``rounding_step="calc_output"``
* ``rule_id`` / ``rules_version``
* ``value_unrounded``：舍入前**全精度**值
* ``value``：按规则舍入后的值
* ``rounding``：``{"scale": int, "mode": str}``

算式来源（优先级）：显式 ``formula`` → 已知 ``op`` → 知识库 ``metric_id`` 的公式。
表达式求值只放行数值、``+ - * /``、括号与已提供的输入名（``ast`` 白名单，绝无 ``eval``）。
"""
from __future__ import annotations

import ast
from decimal import (Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_DOWN,
                     ROUND_HALF_EVEN, ROUND_HALF_UP, ROUND_UP)

from .registry import ToolError, ToolOutcome, ToolSpec, ctx_get, register

ROUNDING_STEP = "calc_output"
_DEFAULT_ROUNDING = {"scale": 2, "mode": "ROUND_HALF_UP"}
_MODES = {
    "ROUND_HALF_UP": ROUND_HALF_UP,
    "ROUND_HALF_EVEN": ROUND_HALF_EVEN,
    "ROUND_HALF_DOWN": ROUND_HALF_DOWN,
    "ROUND_DOWN": ROUND_DOWN,
    "ROUND_UP": ROUND_UP,
}
_ALLOWED_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div)
_ALLOWED_UNARY = (ast.UAdd, ast.USub)
_TRANS = str.maketrans({"×": "*", "÷": "/", "－": "-", "＊": "*", "／": "/", "（": "(", "）": ")"})

SPEC = ToolSpec(
    name="t_compute",
    params={
        "type": "object",
        "properties": {
            "formula": {"type": "object",
                        "description": "{formula_id, expression, inputs, rounding:{scale,mode}, rule_version}"},
            "op": {"type": "string", "description": "已知算式名，如 expected_dividend_yuan"},
            "metric_id": {"type": "string", "description": "用知识库公式"},
            "caliber": {"type": "string"},
            "inputs": {"type": "object", "description": "表达式变量 → 值"},
            "rule_id": {"type": "string"},
            "rules_version": {"type": "string"},
        },
        "required": [],
    },
    receipt={"fields": ["formula_id", "expression", "inputs", "value_unrounded", "value",
                        "rounding", "rounding_step", "rule_id", "rules_version"]},
    preconditions=["给出 formula / op / metric_id 之一与对应 inputs"],
    timeout_s=10,
)


def _to_decimal(value, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ToolError("bad_args", f"{name} 不能是布尔值")
    text = repr(value) if isinstance(value, float) else str(value)
    text = text.replace(",", "").strip()
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        raise ToolError("bad_args", f"{name} 不是合法数值：{value!r}")
    if not number.is_finite():
        raise ToolError("bad_args", f"{name} 必须是有限数")
    return number


def _eval_expression(expr: str, names: dict) -> Decimal:
    """只放行数值常量、``+ - * /``、括号与已提供的输入名（AST 白名单）。"""
    try:
        tree = ast.parse(str(expr).translate(_TRANS), mode="eval")
    except SyntaxError as exc:
        raise ToolError("bad_args", f"表达式无法解析：{exc}")

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.BinOp):
            if not isinstance(node.op, _ALLOWED_BINOPS):
                raise ToolError("bad_args", "表达式含不允许的运算符")
            left, right = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if right == 0:
                raise ToolError("bad_args", "除数为零")
            return left / right
        if isinstance(node, ast.UnaryOp):
            if not isinstance(node.op, _ALLOWED_UNARY):
                raise ToolError("bad_args", "表达式含不允许的一元运算")
            val = ev(node.operand)
            return val if isinstance(node.op, ast.UAdd) else -val
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise ToolError("bad_args", "表达式只允许数值常量")
            return Decimal(str(node.value))
        if isinstance(node, ast.Name):
            if node.id not in names:
                raise ToolError("bad_args", f"表达式引用了未提供的输入：{node.id}")
            return names[node.id]
        raise ToolError("bad_args", f"表达式含不允许的语法：{type(node).__name__}")

    return ev(tree)


def _resolve_formula(args: dict, kb):
    explicit = args.get("formula")
    if isinstance(explicit, dict):
        return dict(explicit)
    op = str(args.get("op", "")).strip()
    metric_id = str(args.get("metric_id", "")).strip()
    caliber = str(args.get("caliber", "")).strip()
    if op == "expected_dividend_yuan":
        return {
            "formula_id": "expected_dividend_yuan",
            "metric_id": "cash_dividend",
            "caliber": "",
            "expression": "shares * per_ten / 10",
            "inputs": ["shares", "per_ten"],
            "rounding": {"scale": 2, "mode": "ROUND_HALF_UP"},
            "rule_version": str(args.get("rules_version") or "1"),
        }
    if metric_id and kb is not None:
        found = kb.formula(metric_id, caliber)
        if found:
            return dict(found)
    return None


def run(args: dict, ctx) -> ToolOutcome:
    kb = ctx_get(ctx, "kb")
    formula = _resolve_formula(args, kb)
    if not formula:
        raise ToolError("bad_args", "缺少 formula / op / metric_id，无法确定算式")

    raw_inputs = args.get("inputs")
    if raw_inputs is None:
        raw_inputs = {}
    if not isinstance(raw_inputs, dict):
        raise ToolError("bad_args", "inputs 必须是对象")

    formula_id = str(formula.get("formula_id") or "")
    names: dict[str, Decimal] = {}

    if formula_id == "expected_dividend_yuan" and "shares" not in raw_inputs:
        shares = raw_inputs.get("shares", raw_inputs.get("total_shares"))
        per_ten = raw_inputs.get("per_ten", raw_inputs.get("dividend_per_ten"))
        if shares is None or per_ten is None:
            raise ToolError("bad_args",
                            "expected_dividend_yuan 需要 shares/total_shares 与 per_ten/dividend_per_ten")
        names = {"shares": _to_decimal(shares, "shares"),
                 "per_ten": _to_decimal(per_ten, "per_ten")}
        expression = "shares * per_ten / 10"
    else:
        for key, val in raw_inputs.items():
            names[str(key)] = _to_decimal(val, str(key))
        expression = str(formula.get("expression") or "")
        if not expression:
            raise ToolError("bad_args", "formula 缺少 expression")

    unrounded = _eval_expression(expression, names)

    rounding = dict(_DEFAULT_ROUNDING)
    provided = formula.get("rounding")
    if isinstance(provided, dict):
        if "digits" in provided and "scale" not in provided:
            rounding["scale"] = provided["digits"]
        for key in ("scale", "mode"):
            if key in provided:
                rounding[key] = provided[key]
    try:
        scale = int(rounding["scale"])
    except (TypeError, ValueError):
        raise ToolError("bad_args", "舍入位数必须是整数")
    if scale < 0:
        raise ToolError("bad_args", "舍入位数不能为负")
    mode = str(rounding["mode"])
    if mode not in _MODES:
        raise ToolError("bad_args", f"不支持的舍入模式：{mode}")

    rounded = unrounded.quantize(Decimal(1).scaleb(-scale), rounding=_MODES[mode])

    rule_id = str(formula.get("rule_id") or formula.get("formula_id")
                  or args.get("rule_id") or "")
    rules_version = str(formula.get("rule_version") or formula.get("rules_version")
                        or args.get("rules_version") or "")
    applicable = formula.get("applicable_when") if isinstance(formula.get("applicable_when"), dict) else {}

    return ToolOutcome(result={
        "formula_id": formula_id,
        "metric_id": str(formula.get("metric_id") or args.get("metric_id") or ""),
        "caliber": str(formula.get("caliber") or ""),
        "expression": expression,
        "inputs": {k: str(v) for k, v in names.items()},
        "value_unrounded": str(unrounded),
        "value": str(rounded),
        "rounding": {"scale": scale, "mode": mode},
        "rounding_step": ROUNDING_STEP,
        "rule_id": rule_id,
        "rules_version": rules_version,
        "unit": str(formula.get("unit") or applicable.get("output_unit") or ""),
    })


register(SPEC, run)

__all__ = ["SPEC", "run", "ROUNDING_STEP"]
