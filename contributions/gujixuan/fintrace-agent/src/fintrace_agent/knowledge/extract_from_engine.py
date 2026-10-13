"""从只读内核 ``fintrace_engine`` 运行时抽取领域知识，落成 ``knowledge/data/*.yaml``。

三条纪律：
1. **不手抄任何数字**：指标表、别名、单位换算倍数、精度、公式全部在运行时从引擎对象现读，
   再落盘。抄写必然抄错，所以代码里没有一个写死的倍数/精度字面量。
2. **幂等**：同一份引擎 → 同一份字节。记录有序、字典键排序、字符串统一双引号。
3. **只读**：只 import 引擎，绝不修改 ``fintrace_engine`` 的任何文件。

产出五份数据文件：``concepts / units / formulas / contexts / relations``。
"""
from __future__ import annotations

import inspect
from decimal import Decimal
from pathlib import Path
from typing import Callable

from fintrace_engine import draft_review as dr
from fintrace_engine.calculators import consistency as consistency_mod

from . import yamlio

#: 知识规则的基础版本号。草稿/权威两档见设计文档 §6.4。
BASE_RULE_VERSION = "1"
#: 生成者签名，写进每份文件头部，便于追来源。
GENERATOR = "fintrace_agent.knowledge.extract_from_engine"

#: 引擎公式的参数名 → 指标键（公式输入的可读映射，不是数值）。
_DIVIDEND_PARAM_METRIC = {
    "shares": "total_shares",
    "dividend_per_ten_shares_yuan": "dividend_per_ten",
}

#: 基准单位 → 定义域（语义映射，不是数值）。
_DOMAIN_BY_BASE = {
    "元": "金额",
    "股": "股数",
    "%": "比率",
    "个百分点": "比率",
    "倍": "倍数",
    "元/股": "每股金额",
    "元/10股": "每10股金额",
}

#: 设计文档 §7 B2/B3 规定的受控词表（非引擎数值，来自规格）。
_EVENT_STATES = ("已实施", "已审议通过", "预案", "计划", "预测")
_STATISTICAL_SCOPES = ("单一方案", "本报告期合计", "多年累计")


def _units_records() -> list[dict]:
    """单位表：来自 MONEY_SCALE / COUNT_SCALE / PASSTHROUGH_UNITS / PRECISION。"""
    rows: dict[str, dict] = {}

    def add(unit: str, base: str, scale: float) -> None:
        rows[unit] = {
            "unit": unit,
            "base": base,
            "scale": float(scale),
            # 精度按基准单位取（引擎 to_base 后按基准单位读小数位）
            "precision": int(dr.PRECISION.get(base, 2)),
            "rounding": "ROUND_HALF_UP",
        }

    for unit, scale in dr.MONEY_SCALE.items():
        add(unit, "元", scale)
    for unit, scale in dr.COUNT_SCALE.items():
        add(unit, "股", scale)
    for unit in dr.PASSTHROUGH_UNITS:
        add(unit, unit, 1.0)
    # 兜底：PRECISION 里若有未被上面覆盖的单位，也补上（当前为空操作）
    for unit in dr.PRECISION:
        if unit not in rows:
            add(unit, unit, 1.0)
    return [rows[u] for u in sorted(rows)]


def _caliber_hint(spec: dict) -> str:
    """从概念定义文本推出「口径提示」——扣非 / 归母 / 空。"""
    text = spec["cn"] + "".join(spec["aliases"])
    if "扣除非经常性损益" in text or "扣非" in text:
        return "扣非"
    if "归属于上市公司股东" in text or "归属于母公司" in text or "归母" in text:
        return "归母"
    return ""


def _concepts_records() -> list[dict]:
    out: list[dict] = []
    for metric_id, spec in dr.METRICS.items():
        base = spec["unit"]
        out.append({
            "metric_id": metric_id,
            "cn": spec["cn"],
            "aliases": list(spec["aliases"]),
            "base_unit": base,
            "domain": _DOMAIN_BY_BASE.get(base, base),
            "caliber_hint": _caliber_hint(spec),
            "version": BASE_RULE_VERSION,
            "status": "authoritative",
        })
    return out


def _rounding_probe(fn: Callable[..., Decimal]) -> tuple[int, str]:
    """从函数行为反推舍入规则（不抄代码注释）：

    - 小数位：``fn(1, 1) = 0.10`` → 结果指数 ``-2`` → 2 位；
    - 舍入模式：``fn(1, 0.05) = 0.005`` 正好落在分位半格 ——
      ROUND_HALF_UP 得 ``0.01``，ROUND_HALF_EVEN 得 ``0.00``。
    """
    scale = -fn(Decimal("1"), Decimal("1")).as_tuple().exponent
    probe = fn(Decimal("1"), Decimal("0.05"))
    if probe == Decimal("0.01"):
        mode = "ROUND_HALF_UP"
    elif probe == Decimal("0.00"):
        mode = "ROUND_HALF_EVEN"
    else:
        mode = "UNKNOWN"
    return int(scale), mode


def _formulas_records() -> list[dict]:
    """公式表：来自 ``calculators.consistency`` 的分红复算定义。"""
    fn = consistency_mod.expected_dividend_yuan
    sig = inspect.signature(fn)
    inputs = [_DIVIDEND_PARAM_METRIC[p] for p in sig.parameters]
    scale, mode = _rounding_probe(fn)
    doc = (fn.__doc__ or "").strip().splitlines()[0]
    # 表达式取 docstring 首行「<名>：<表达式>，<说明>」里 <表达式> 那一段（运行时抽取）
    expression = doc.split("：", 1)[1].split("，", 1)[0] if "：" in doc else ""
    metric_id = sorted(k for k, s in dr.METRICS.items() if s.get("rule") == "dividend")[0]
    base_unit = dr.METRICS[metric_id]["unit"]
    return [{
        "formula_id": fn.__name__,
        "metric_id": metric_id,
        "caliber": "",
        "expression": expression,
        "inputs": inputs,
        "rounding": {"scale": scale, "mode": mode},
        "applicable_when": {"metric_id": metric_id, "output_unit": base_unit},
        "rule_version": BASE_RULE_VERSION,
        "status": "authoritative",
    }]


def _contexts_records() -> list[dict]:
    """上下文受控词表：口径 / 单位 / 期间种类 / 指标处理指令 / B2 事件状态 / B3 统计范围。"""
    out: list[dict] = []
    # 口径：来自引擎的 _CALIBER_RX（扣非 / 归母）
    for name, rx in dr._CALIBER_RX:
        out.append({
            "context_id": f"caliber:{name}",
            "kind": "caliber",
            "value": name,
            "note": f"引擎 _CALIBER_RX 模式: {rx.pattern}",
        })
    # 单位
    for r in _units_records():
        out.append({
            "context_id": f"unit:{r['unit']}",
            "kind": "unit",
            "value": r["unit"],
            "note": f"基准 {r['base']}，倍数 {r['scale']:g}，精度 {r['precision']}",
        })
    # 期间种类（引擎 _text_period_kind 支持的归一形态）
    for kind, note in (
        ("A", "年度，如 2023A"),
        ("H1", "上半年，如 2023H1"),
        ("H2", "下半年，如 2023H2"),
        ("Q1", "一季度，如 2023Q1"),
        ("Q2", "二季度，如 2023Q2"),
        ("Q3", "三季度，如 2023Q3"),
        ("Q4", "四季度，如 2023Q4"),
    ):
        out.append({"context_id": f"period_kind:{kind}", "kind": "period_kind",
                    "value": kind, "note": note})
    # 指标处理指令：来自引擎 METRICS[key].get("rule")
    for metric_id, spec in dr.METRICS.items():
        rule = spec.get("rule")
        if rule:
            out.append({"context_id": f"metric_rule:{metric_id}", "kind": "metric_rule",
                        "value": metric_id, "note": rule})
    # 受控词表（设计文档 §7 B2/B3）
    for value in _EVENT_STATES:
        out.append({"context_id": f"event_state:{value}", "kind": "event_state",
                    "value": value, "note": "设计 §7 B2 事件状态"})
    for value in _STATISTICAL_SCOPES:
        out.append({"context_id": f"scope:{value}", "kind": "statistical_scope",
                    "value": value, "note": "设计 §7 B3 统计范围"})
    return out


def _relations_records() -> list[dict]:
    """关系表：指标键等价（来自 METRIC_KEY_ALIASES）+ 公式的输入依赖（来自公式表）。"""
    out: list[dict] = []
    for alias, canonical in dr.METRIC_KEY_ALIASES.items():
        out.append({
            "rule_id": f"metric_key_alias:{alias}",
            "predicate": "equals",
            "subject_metric": alias,
            "object_metric": canonical,
            "applicable_when": {"source": "METRIC_KEY_ALIASES"},
            "rule_version": BASE_RULE_VERSION,
            "status": "authoritative",
        })
    for f in _formulas_records():
        for inp in f["inputs"]:
            out.append({
                "rule_id": f"{f['formula_id']}:{f['metric_id']}->{inp}",
                "predicate": "derives_from",
                "subject_metric": f["metric_id"],
                "object_metric": inp,
                "applicable_when": {"formula_id": f["formula_id"]},
                "rule_version": f["rule_version"],
                "status": "authoritative",
            })
    return out


#: 数据文件 → 记录生成函数
_FILES: dict[str, Callable[[], list[dict]]] = {
    "concepts": _concepts_records,
    "units": _units_records,
    "formulas": _formulas_records,
    "contexts": _contexts_records,
    "relations": _relations_records,
}


def build_records() -> dict[str, dict]:
    """返回五份数据文件的完整文档（含头部元信息）。"""
    return {
        name: {
            "schema_version": 1,
            "kind": name,
            "generator": GENERATOR,
            "rule_version": BASE_RULE_VERSION,
            "records": fn(),
        }
        for name, fn in _FILES.items()
    }


def generate(yaml_dir: str | Path) -> list[Path]:
    """把五份数据文件写到 ``yaml_dir``，返回写入的路径列表。幂等。"""
    yaml_dir = Path(yaml_dir)
    yaml_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, payload in build_records().items():
        path = yaml_dir / f"{name}.yaml"
        # newline="" 关掉行尾翻译：文件统一 LF，跨平台与两次生成都字节一致
        path.write_text(yamlio.dumps(payload), encoding="utf-8", newline="")
        written.append(path)
    return written


def main() -> int:
    from .. import paths

    for path in generate(paths.KNOWLEDGE_DATA):
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
