"""分红披露一致性判定：算术走 calculators.consistency，这里只做分级与证据归集。

判定纪律（沿用项目既有口径）：
- 只有「同一份材料内部两处对不上」才给 error；
- 格式类问题（小数位、单位）给 warn；
- 口径说不清的给 info，并在文字里写清「口径待确认」，绝不冒充结论。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from ..calculators.consistency import expected_dividend_yuan
from ..parsers.dividend_extract import DividendSnapshot, Field

WAN = Decimal("10000")


@dataclass(frozen=True)
class Evidence:
    """一条发现的出处：哪份文件、第几页、原文哪一行、值是多少。"""

    file: str
    page: int | None
    line: str
    value: str | None = None

    def to_dict(self) -> dict:
        return {"file": self.file, "page": self.page, "line": self.line, "value": self.value}

    def text(self) -> str:
        pg = f"第 {self.page} 页" if self.page else "页码未取到"
        val = f"　值 = {self.value}" if self.value is not None else ""
        return f"{self.file} {pg}：{self.line}{val}"


@dataclass(frozen=True)
class Finding:
    code: str
    title: str
    severity: str          # error / warn / info
    detail: str
    evidence: tuple[Evidence, ...] = ()

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "title": self.title,
            "severity": self.severity,
            "detail": self.detail,
            "evidence": [e.to_dict() for e in self.evidence],
        }


def evidence_of(f: Field, value=None) -> Evidence:
    return Evidence(
        file=f.source_file,
        page=f.page,
        line=f.label_line or f.value_line,
        value=str(value) if value is not None else (str(f.value) if f.value is not None else None),
    )


def decimals_of(value: Decimal) -> int:
    exp = value.as_tuple().exponent
    return -exp if isinstance(exp, int) and exp < 0 else 0


def _pick(primary: Field, fallback: Field) -> Field:
    return primary if primary.ok else fallback


def check_dividend(snap: DividendSnapshot) -> tuple[list[Finding], dict]:
    """核查一份年报内部的现金分红披露是否自洽。

    返回 (findings, meta)；meta 里带复算过程用到的原始取值与来源，供报告复述。
    """
    findings: list[Finding] = []

    base_field = _pick(snap.base, snap.narr_base)
    per10_field = _pick(snap.per10, snap.narr_per10)

    meta: dict = {
        "base": base_field.value,
        "base_source": evidence_of(base_field).text() if base_field.ok else None,
        "per10": per10_field.value,
        "per10_source": evidence_of(per10_field).text() if per10_field.ok else None,
        "formula": "股本基数 × 每10股派息数 ÷ 10（ROUND_HALF_UP 到分）",
    }

    if not (base_field.ok and per10_field.ok):
        findings.append(
            Finding(
                code="D0",
                title="关键字段未取到，无法复算",
                severity="warn",
                detail=(
                    "股本基数或每 10 股派息数没有从原文取到（不是 0，是没有）。"
                    "按纪律不用别处的数字顶替，因此本次不对分红一致性下任何判断。"
                ),
                evidence=tuple(
                    evidence_of(f) for f in (snap.base, snap.narr_base, snap.per10, snap.narr_per10)
                ),
            )
        )
        return findings, meta

    expected = expected_dividend_yuan(base_field.value, per10_field.value)
    meta["expected"] = expected
    meta["expected_text"] = f"{expected:,}"

    # D1：表内「现金分红金额」与复算值是否一致
    for label, field in (
        ("现金分红金额（元）（含税）", snap.cash_amount),
        ("现金分红总额（含其他方式）（元）", snap.cash_total),
    ):
        if not field.ok:
            findings.append(
                Finding(
                    code="D1-missing",
                    title=f"{label} 未取到",
                    severity="info",
                    detail="该行没有从原文取到数值，未参与判定。",
                    evidence=(evidence_of(field),),
                )
            )
            continue
        if field.value != expected:
            ratio = (expected / field.value).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            hint = ""
            if ratio == Decimal("10.0000"):
                hint = "复算值正好是披露值的 10 倍 —— 典型的金额位序/小数点错一位。"
            elif ratio.copy_abs() != Decimal("1.0000"):
                hint = f"两者比值 {ratio}，不是舍入误差能解释的差。"
            findings.append(
                Finding(
                    code="D1-inconsistent",
                    title=f"同一份年报内分红金额对不上：{label}",
                    severity="error",
                    detail=(
                        f"按「{base_field.value:,} 股 × {per10_field.value} 元 ÷ 10」复算，应为 "
                        f"{expected:,} 元；但该行披露为 {field.value:,} 元。{hint}"
                    ),
                    evidence=(
                        evidence_of(base_field),
                        evidence_of(per10_field),
                        evidence_of(field),
                    ),
                )
            )
        else:
            findings.append(
                Finding(
                    code="D1-ok",
                    title=f"{label} 与复算值一致",
                    severity="info",
                    detail=f"复算 {expected:,} 元，与该行披露值一致。",
                    evidence=(evidence_of(field),),
                )
            )

    # D2：金额精确到分是分红披露的常态，出现三位小数属于格式异常
    for label, field in (
        ("现金分红金额（元）（含税）", snap.cash_amount),
        ("现金分红总额（含其他方式）（元）", snap.cash_total),
    ):
        if field.ok and decimals_of(field.value) > 2:
            findings.append(
                Finding(
                    code="D2-format",
                    title=f"{label} 小数位异常",
                    severity="warn",
                    detail=(
                        f"披露值为 {field.value}，有 {decimals_of(field.value)} 位小数；"
                        "现金分红金额通常只精确到分，多出来的位数本身就是可疑信号。"
                    ),
                    evidence=(evidence_of(field),),
                )
            )

    # D3：叙述段的「万元」总额与复算值按万元保留两位四舍五入后是否一致
    if snap.narr_total_wan.ok:
        disclosed_wan = snap.narr_total_wan.value
        other_rounded = (expected / WAN).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if disclosed_wan == other_rounded:
            findings.append(
                Finding(
                    code="D3-ok",
                    title="叙述段总额与复算值一致（万元口径）",
                    severity="info",
                    detail=(
                        f"叙述段披露 {disclosed_wan} 万元，复算值 {expected:,} 元折合 "
                        f"{other_rounded} 万元，按万元保留两位四舍五入后一致。"
                    ),
                    evidence=(evidence_of(snap.narr_total_wan),),
                )
            )
        else:
            findings.append(
                Finding(
                    code="D3-inconsistent",
                    title="叙述段总额与复算值对不上",
                    severity="error",
                    detail=(
                        f"叙述段披露 {disclosed_wan} 万元，复算值 {expected:,} 元折合 "
                        f"{other_rounded} 万元，两者在万元口径下也不一致。"
                    ),
                    evidence=(
                        evidence_of(snap.narr_total_wan),
                        evidence_of(base_field),
                        evidence_of(per10_field),
                    ),
                )
            )

    # D4：叙述段的股本基数/派息与表格取值是否一致（同一份材料两处口径应相同）
    for name, table_field, narr_field in (
        ("股本基数", snap.base, snap.narr_base),
        ("每10股派息数", snap.per10, snap.narr_per10),
    ):
        if table_field.ok and narr_field.ok and table_field.value != narr_field.value:
            findings.append(
                Finding(
                    code="D4-inconsistent",
                    title=f"{name} 在表格与叙述段不一致",
                    severity="error",
                    detail=f"表格为 {table_field.value}，叙述段为 {narr_field.value}。",
                    evidence=(evidence_of(table_field), evidence_of(narr_field)),
                )
            )

    # D5：占比口径提示（不下结论）
    if snap.distributable.ok and snap.cash_total.ok and snap.cash_total.value != 0:
        share = (snap.cash_total.value / snap.distributable.value * 100).quantize(Decimal("0.1"))
        findings.append(
            Finding(
                code="D5-info",
                title="占比口径提示（需人工确认口径，非结论）",
                severity="info",
                detail=(
                    f"该页披露可分配利润 {snap.distributable.value:,} 元，现金分红总额 "
                    f"{snap.cash_total.value} 元，两者比值为 {share}%。若「利润分配总额」指的是"
                    "可分配利润，那么页面上「占比 100%」的说法与这两个数对不上；若「利润分配总额」"
                    "另有所指（例如就等于本次现金分红），则 100% 成立。口径未确认前，只作提示，"
                    "不判定为差错。"
                ),
                evidence=(evidence_of(snap.distributable), evidence_of(snap.cash_total)),
            )
        )

    return findings, meta


def notice_ground_truth(notice_pages, expected: Decimal | None) -> dict:
    """在官方更正公告里找复算值的落地证据（只做字面检索，不做推断）。

    仅用于事后检验「检出是否与官方更正一致」，不参与本链路的事前判定。
    """
    if expected is None:
        return {"checked": False, "reason": "没有复算值"}
    with_commas = f"{expected:,}"
    plain = f"{expected}"
    for page in notice_pages:
        for needle in (with_commas, plain):
            if needle in page.flat:
                return {
                    "checked": True,
                    "hit": True,
                    "value": str(expected),
                    "page": page.page,
                    "file": page.path.name,
                    "matched": needle,
                }
    return {"checked": True, "hit": False, "value": str(expected), "reason": "公告文本中未出现该数值"}
