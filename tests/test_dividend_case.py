"""最小链路测试：PDF 页码定位 → 字段抽取 → 确定性复算 → 可追溯报告。

不依赖 pymupdf 的部分用构造页跑；真实 PDF 存在时跑端到端，并与「人工摘录路径」
（data/demo/qiming_2023_dividend.json → tools/run_qiming_demo.py）做回归比对：
两条路径必须给出同一个复算值。
"""
from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path

import pytest

from fintrace.calculators.consistency import dividend_reconciliation, expected_dividend_yuan
from fintrace.parsers import dividend_extract as dx
from fintrace.parsers.pdf_pages import PageText
from fintrace.verification import dividend_checks as dc

ROOT = Path(__file__).resolve().parents[1]
DEMO_CASE = json.loads((ROOT / "data/demo/qiming_2023_dividend.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- 构造页

def _page(page: int, raw: str) -> PageText:
    return PageText(path=Path("改错前_2023年年度报告.pdf"), page=page, raw=raw)


def _fake_pages() -> list[PageText]:
    return [
        _page(
            51,
            "\n".join(
                [
                    "二、利润分配预案",
                    "分配预案的股本基数（股）",
                    "408,548,455",
                    "每10股派息数（元）（含税）",
                    "0.10",
                    "现金分红金额（元）（含税）",
                    "408,548.46",
                    "现金分红总额（含其他方式）（元）",
                    "408,548.455",
                    "可分配利润（元）",
                    "408,548,455.00",
                ]
            ),
        ),
        _page(
            223,
            "\n".join(
                [
                    "（三）期后事项",
                    "以公司现有总股本408,548,455股为基数，每10股派发现金红利0.10元，",
                    "利润分配总额为408.55万元。",
                ]
            ),
        ),
    ]


# --------------------------------------------------------------------------- 算术：单一来源

def test_expected_dividend_yuan_is_the_single_arithmetic_source():
    assert expected_dividend_yuan(408548455, "0.10") == Decimal("4085484.55")


def test_manual_path_and_pdf_path_share_the_same_expected_value():
    """人工摘录路径（组长的 demo）与 PDF 路径的复算值必须相同。"""
    from tools.run_qiming_demo import build

    manual = build(DEMO_CASE)["detection"]["calculation"]
    assert manual["expected_yuan"] == str(expected_dividend_yuan(
        DEMO_CASE["observations"]["shares"]["value"],
        DEMO_CASE["observations"]["dividend_per_ten_shares"]["value"],
    ))
    assert manual["expected_yuan"] == "4085484.55"


def test_dividend_reconciliation_behaviour_unchanged():
    result = dividend_reconciliation(408548455, "0.10", "408548.46", "408.55")
    assert result["expected_yuan"] == "4085484.55"
    assert result["disclosed_yuan"] == "408548.46"
    assert result["difference_yuan"] == "3676936.09"
    assert result["disclosure_matches"] is False
    assert result["other_page_matches_after_rounding"] is True


# --------------------------------------------------------------------------- 抽取 + 判定

def test_extract_pulls_every_field_with_its_page():
    snap = dx.extract_dividend(_fake_pages())
    assert snap.base.value == Decimal("408548455") and snap.base.page == 51
    assert snap.per10.value == Decimal("0.10") and snap.per10.page == 51
    assert snap.cash_amount.value == Decimal("408548.46")
    assert snap.narr_total_wan.value == Decimal("408.55") and snap.narr_total_wan.page == 223
    assert snap.pages_hit["利润分配表页"] == [51]


def test_checks_flag_inconsistent_with_page_evidence():
    findings, meta = dc.check_dividend(dx.extract_dividend(_fake_pages()))
    codes = [f.code for f in findings]
    assert "D1-inconsistent" in codes
    assert meta["expected"] == Decimal("4085484.55")

    bad = next(f for f in findings if f.code == "D1-inconsistent")
    assert bad.severity == "error"
    assert "4085484.55" in bad.detail.replace(",", "")
    pages = {e.page for e in bad.evidence}
    assert pages == {51}


def test_format_anomaly_is_warn_not_error():
    findings, _ = dc.check_dividend(dx.extract_dividend(_fake_pages()))
    fmt = [f for f in findings if f.code == "D2-format"]
    assert fmt and all(f.severity == "warn" for f in fmt)
    assert any("408548.455" in (f.detail or "") for f in fmt)


def test_missing_field_never_guesses():
    pages = [_page(51, "二、利润分配预案\n每10股派息数（元）（含税）\n0.10")]
    findings, meta = dc.check_dividend(dx.extract_dividend(pages))
    assert [f.code for f in findings] == ["D0"]
    assert findings[0].severity == "warn"
    assert "expected" not in meta


# --------------------------------------------------------------------------- 端到端（需真实 PDF）

def _case_dir() -> Path | None:
    candidates = []
    if os.environ.get("FINTRACE_CASE_DIR"):
        candidates.append(Path(os.environ["FINTRACE_CASE_DIR"]))
    candidates.append(ROOT / "data" / "raw" / "002232")
    for c in candidates:
        if c.is_dir() and (c / "01_改错前_2023年年度报告.pdf").exists():
            return c
        if c.is_dir() and (c / "2023年年度报告.pdf").exists():
            return c
    return None


def _pdfs(case_dir: Path) -> tuple[Path, Path | None]:
    before = case_dir / "01_改错前_2023年年度报告.pdf"
    if not before.exists():
        before = case_dir / "2023年年度报告.pdf"
    after = case_dir / "03_改错后_2023年年度报告_更正后.pdf"
    if not after.exists():
        after = case_dir / "启明信息技术股份有限公司2023年年度报告（更正后）.pdf"
    notice = case_dir / "02_官方更正公告.pdf"
    if not notice.exists():
        notice = case_dir / "关于2023年年度报告的更正公告.pdf"
    return before, (notice if notice.exists() else None)


def test_pdf_end_to_end_and_no_false_positive_after_correction():
    pytest.importorskip("pymupdf")
    case_dir = _case_dir()
    if case_dir is None:
        pytest.skip("本机没有启明年报 PDF（设 FINTRACE_CASE_DIR 或放到 data/raw/002232/）")

    from fintrace.dividend_case import run_pdf_case

    before, notice = _pdfs(case_dir)

    # 改错前：应当报出不一致，且复算值与人工摘录路径相同
    res = run_pdf_case(
        before, cutoff_date="2024-04-01", publication_date="2024-03-29",
        company="启明信息", case_id="qiming-2023-dividend", notice_path=notice,
    )
    assert res.errors, "改错前年报应当报出不一致"
    assert res.payload["recalculation"]["expected_yuan"] == "4085484.55"
    assert res.payload["conclusion"]["status"] == "REVIEW_REQUIRED"
    assert 51 in res.pages_hit["利润分配表页"]
    assert res.notice.get("hit") is True, "官方更正公告里应能找到复算值"
    assert res.records and res.records[0].label == "RISK_SIGNAL_DETECTED"
    assert all(
        e.available_before_cutoff is True for r in res.records for e in r.evidence
    ), "进入检测的证据必须都在截止日之前"

    # 与人工摘录路径的回归：两条路径必须给出同一个数
    from tools.run_qiming_demo import build

    manual = build(DEMO_CASE)["detection"]["calculation"]
    assert manual["expected_yuan"] == res.payload["recalculation"]["expected_yuan"]
    d1 = next(f for f in res.findings if f.code == "D1-inconsistent")
    assert manual["disclosed_yuan"] == d1.evidence[2].value
    assert manual["difference_yuan"] == str(
        Decimal(res.payload["recalculation"]["expected_yuan"]) - Decimal(manual["disclosed_yuan"])
    )

    # 改错后：同一套规则不应报任何不一致
    after = case_dir / "03_改错后_2023年年度报告_更正后.pdf"
    if not after.exists():
        after = case_dir / "启明信息技术股份有限公司2023年年度报告（更正后）.pdf"
    res_after = run_pdf_case(
        after, cutoff_date="2024-04-30", publication_date="2024-04-23", company="启明信息",
    )
    assert res_after.errors == [], f"更正后不应报不一致：{[f.code for f in res_after.errors]}"
    assert res_after.payload["conclusion"]["status"] == "CONSISTENT"
    assert res_after.records == []


def test_material_after_cutoff_is_rejected():
    pytest.importorskip("pymupdf")
    case_dir = _case_dir()
    if case_dir is None:
        pytest.skip("本机没有启明年报 PDF")

    from fintrace.dividend_case import run_pdf_case

    before, _ = _pdfs(case_dir)
    with pytest.raises(ValueError, match="拒绝进入检测输入"):
        run_pdf_case(before, cutoff_date="2024-03-01", publication_date="2024-03-29")
