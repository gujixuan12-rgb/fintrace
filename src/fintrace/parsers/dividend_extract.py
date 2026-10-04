"""从原始 PDF 文本里抽取分红字段，每个字段都带上「页码 + 原文行」。

原则：宁可抽不到（返回 None）也不要抽错。抽不到时报告里会显式写「未取到」，
绝不用其它位置的数字顶替。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from .pdf_pages import PageText, find_pages

_NUM_LINE = re.compile(r"^[-+]?[\d,，]+(?:\.\d+)?$")
_HAS_CJK = re.compile(r"[\u4e00-\u9fff]")


def norm_number(text: str) -> Decimal:
    """把中文年报里的数字写法（含千分位、空格、全角逗号）转成 Decimal。"""
    s = text.strip().replace("，", ",").replace(" ", "")
    s = s.replace(",", "")
    try:
        return Decimal(s)
    except InvalidOperation as exc:  # pragma: no cover - 由调用方兜底
        raise ValueError(f"不是合法数字：{text!r}") from exc


def is_number_line(line: str) -> bool:
    return bool(_NUM_LINE.match(line.strip().replace(" ", "")))


@dataclass(frozen=True)
class Field:
    """一个带出处的字段取值。"""

    name: str
    value: Decimal | None
    page: int | None
    label_line: str
    value_line: str
    source_file: str

    @property
    def ok(self) -> bool:
        return self.value is not None

    def evidence(self) -> dict:
        return {
            "field": self.name,
            "value": str(self.value) if self.value is not None else None,
            "file": self.source_file,
            "page": self.page,
            "label_line": self.label_line,
            "value_line": self.value_line,
        }


def label_value(pages: list[PageText], label: str, window: int = 1) -> Field:
    """找到 label 行，取紧随其后的纯数字行作为取值。

    window=1 表示只看紧邻的下一行 —— 年报表格里标签和数值基本是相邻行，
    放宽反而会串行取到别的字段（例如把「股本基数」的数值当成「每10股派息数」）。
    """
    for page in pages:
        lines = page.lines
        for i, line in enumerate(lines):
            if "".join(label.split()) not in "".join(line.split()):
                continue
            for j in range(i + 1, min(i + 1 + window, len(lines))):
                cand = lines[j]
                if is_number_line(cand) and not _HAS_CJK.search(cand):
                    return Field(
                        name=label,
                        value=norm_number(cand),
                        page=page.page,
                        label_line=line,
                        value_line=cand,
                        source_file=page.path.name,
                    )
            return Field(
                name=label,
                value=None,
                page=page.page,
                label_line=line,
                value_line="",
                source_file=page.path.name,
            )
    return Field(name=label, value=None, page=None, label_line="", value_line="", source_file="")


# ---------------------------------------------------------------- 叙述段抽取

_NARR_BASE = re.compile(r"总股本([\d,，]+)股为基数")
_NARR_PER10 = re.compile(r"每10股(?:派发现金红利|分配|派息)[^\d]{0,6}([\d．.]{1,8})元")


def narrative_stock_base(pages: list[PageText]) -> Field:
    """从「以…总股本 X 股为基数」这类叙述句里取股本基数。"""
    for page in pages:
        m = _NARR_BASE.search(page.flat)
        if m:
            raw = m.group(1)
            return Field(
                name="总股本（叙述段）",
                value=norm_number(raw),
                page=page.page,
                label_line=m.group(0),
                value_line=raw,
                source_file=page.path.name,
            )
    return Field("总股本（叙述段）", None, None, "", "", "")


def narrative_per10(pages: list[PageText]) -> Field:
    """从叙述句里取「每 10 股派 X 元」。"""
    for page in pages:
        m = _NARR_PER10.search(page.flat)
        if m:
            raw = m.group(1)
            return Field(
                name="每10股派息数（叙述段）",
                value=norm_number(raw),
                page=page.page,
                label_line=m.group(0),
                value_line=raw,
                source_file=page.path.name,
            )
    return Field("每10股派息数（叙述段）", None, None, "", "", "")


_WAN = re.compile(r"利润分配总额为([\d,，]+(?:[．.][\d]+)?)万元")


def narrative_total_wan(pages: list[PageText]) -> Field:
    """从「利润分配总额为 X 万元」取总额（单位：万元，原样保留，不换算）。"""
    for page in pages:
        m = _WAN.search(page.flat)
        if m:
            raw = m.group(1)
            return Field(
                name="利润分配总额（万元，叙述段）",
                value=norm_number(raw),
                page=page.page,
                label_line=m.group(0),
                value_line=raw,
                source_file=page.path.name,
            )
    return Field("利润分配总额（万元，叙述段）", None, None, "", "", "")


@dataclass
class DividendSnapshot:
    """一份年报里与现金分红有关、且带出处的全部取值。"""

    file: str
    base: Field
    per10: Field
    cash_amount: Field
    cash_total: Field
    distributable: Field
    narr_base: Field
    narr_per10: Field
    narr_total_wan: Field
    pages_hit: dict = field(default_factory=dict)

    def all_fields(self) -> list[Field]:
        return [
            self.base,
            self.per10,
            self.cash_amount,
            self.cash_total,
            self.distributable,
            self.narr_base,
            self.narr_per10,
            self.narr_total_wan,
        ]


def extract_dividend(pages: list[PageText]) -> DividendSnapshot:
    """抽出分红核查需要的全部字段（表格 + 叙述段）。"""
    return DividendSnapshot(
        file=pages[0].path.name if pages else "",
        base=label_value(pages, "分配预案的股本基数（股）"),
        per10=label_value(pages, "每10股派息数（元）（含税）"),
        cash_amount=label_value(pages, "现金分红金额（元）（含税）"),
        cash_total=label_value(pages, "现金分红总额（含其他方式）（元）"),
        distributable=label_value(pages, "可分配利润（元）"),
        narr_base=narrative_stock_base(pages),
        narr_per10=narrative_per10(pages),
        narr_total_wan=narrative_total_wan(pages),
        pages_hit={
            "利润分配表页": [p.page for p in find_pages(pages, "现金分红金额")],
            "期后事项页": [p.page for p in find_pages(pages, "派发现金红利")],
        },
    )
