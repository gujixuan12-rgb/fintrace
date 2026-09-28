"""步骤 ⑧ 的守门人：措辞约束（wording policy）。

陈翊民 2026-09-26 原文：
    Agent 只能输出：
    - "存在风险信号" / "建议进一步核查" / "可能涉及某类风险"
    - "当前公开信息不足以确认"
    不能直接输出：
    - "公司虚增收入" / "公司财务造假" / "供应商交易虚假"
    除非输入中已经存在监管或公司正式确认文件，而且当前任务明确属于事后核查。

实现要点：**约束写在代码里，不写在提示词里。**
模型可以自由生成草稿，但报告必须过 `assert_clean()` 才能离开系统。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

#: 允许的核查标签。事前预警只允许前四个。
ALLOWED_LABELS: dict[str, str] = {
    "RISK_SIGNAL_DETECTED": "存在风险信号",
    "FURTHER_VERIFICATION_SUGGESTED": "建议进一步核查",
    "POSSIBLE_RISK_CATEGORY": "可能涉及某类风险",
    "INSUFFICIENT_INFORMATION": "当前公开信息不足以确认",
}

#: 只有事后核查（且已有监管/公司确认文件）才允许的标签。
POST_HOC_LABELS: dict[str, str] = {
    "CONFIRMED_BY_AUTHORITATIVE_DOCUMENT": "已由监管或公司正式文件确认",
}

#: 定性断言黑名单。命中即视为越界，无论上下文。
#: 关键词只覆盖「对主体的定性」，不覆盖「说明某类风险机制」的一般名词。
BANNED_PATTERNS: list[tuple[str, str]] = [
    (r"虚增(收入|利润|资产|营收|业绩)", "对虚增行为的定性断言"),
    (r"财务造假|财务舞弊|会计造假", "对财务造假的定性断言"),
    (r"(虚假记载|虚假陈述|严重失实)", "对披露真实性的定性断言"),
    (r"供应商交易(虚假|不实|虚构)", "对具体交易真实性的定性断言"),
    (r"(伪造|编造)(合同|凭证|单据|发票)", "对凭证真实性的定性断言"),
    (r"(侵占|挪用|掏空)(公司)?(资金|资产)", "对资金去向的定性断言"),
    (r"(操纵|干预)(股价|利润|报表)", "对操纵行为的定性断言"),
    (r"(关联方)?(利益输送)", "对利益输送的定性断言"),
    (r"(违法违规|涉嫌犯罪|已被立案)", "对法律定性的断言"),
]

_COMPILED = [(re.compile(p), why) for p, why in BANNED_PATTERNS]

#: 推荐用词（把定性断言替换成风险信号的写法），供文档与前端提示使用。
PREFERRED_PHRASING = {
    "虚增收入": "营业收入与相关现金流、关联交易结构存在不匹配，存在风险信号",
    "财务造假": "多项会计估计与同业偏离，建议进一步核查",
    "供应商交易虚假": "主要供应商的公开信息、成立时间与交易规模存在不匹配，可能涉及交易真实性风险",
}


@dataclass
class Violation:
    pattern: str
    why: str
    excerpt: str

    def __str__(self) -> str:
        return f"{self.why}：…{self.excerpt}…"


def scan(text: str) -> list[Violation]:
    """扫描一段文本里的定性断言。"""
    out: list[Violation] = []
    for rx, why in _COMPILED:
        for m in rx.finditer(text or ""):
            lo, hi = max(0, m.start() - 20), min(len(text), m.end() + 20)
            out.append(Violation(rx.pattern, why, text[lo:hi].replace("\n", " ")))
    return out


def is_allowed_label(label: str, mode: str = "pre_hoc", has_confirmation: bool = False) -> bool:
    if label in ALLOWED_LABELS:
        return True
    if label in POST_HOC_LABELS:
        return mode == "post_hoc" and has_confirmation
    return False


def assert_clean(
    texts: list[str],
    mode: str = "pre_hoc",
    has_confirmation: bool = False,
    label: str | None = None,
) -> None:
    """报告出场前的最后一道门。不合规直接抛错，不生成半成品。

    Args:
        texts: 报告中所有面向用户的自然语言字段。
        mode: pre_hoc | post_hoc。
        has_confirmation: 输入材料里是否存在监管/公司正式确认文件。
        label: 该报告的核查标签，若提供则一并校验。
    """
    if mode == "pre_hoc" and has_confirmation:
        # 事前预警允许用到已发生的监管文件，但不允许因此升级为定性结论
        has_confirmation = False
    if label is not None and not is_allowed_label(label, mode, has_confirmation):
        raise WordingViolation(
            f"核查标签 {label!r} 在当前模式（{mode}，确认文件={has_confirmation}）下不允许输出"
        )
    hits: list[Violation] = []
    for t in texts:
        hits.extend(scan(t))
    if hits:
        detail = "；".join(str(h) for h in hits[:5])
        raise WordingViolation(f"检测到 {len(hits)} 处定性断言，违反措辞约束：{detail}")


class WordingViolation(ValueError):
    """措辞约束被打破。"""
