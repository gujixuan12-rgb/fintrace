"""草稿核查闭环的回归测试。

重点锁住三类曾经出错的地方：
1. PDF 表格相邻列粘连时，数字不能把邻居的首位吃掉（`14.17%` 被吃成 `17%`）；
2. 「元」与「元/股」这类口径混用不能被判成单位错（那是量级差）；
3. 证据引用要指向含该数值的那一行，而不是表头。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fintrace import draft_review as dr

ROOT = Path(__file__).resolve().parents[1]
CASE_PATH = ROOT / "data/annotations/cases/qiming_2023_draft_case.json"
ANNUAL_REPORT = ROOT / "data/raw/002232/01_改错前_2023年年度报告.pdf"


def _page(raw: str, page: int = 1) -> dr.PageRec:
    return dr.PageRec(
        source_id="s", file_name="f.pdf", source_type="t",
        publication_date="2024-04-13", page=page, raw=raw,
    )


# --- 数值抽取：表格粘连 ---------------------------------------------------

def test_粘连的相邻列不会吞掉下一个数():
    """`1,064,066,560.8814.17%` → 必须拆成 1,064,066,560.88 和 14.17%。"""
    page = _page("毛利率1,239,720,956.431,064,066,560.8814.17%-19.43%")
    toks = dr.tokens_near(page, ["毛利率"], base_unit="%")
    pct = [t.value for t in toks if t.unit == "%"]
    money = [t.raw for t in toks if t.unit == ""]
    assert 14.17 in pct
    assert 17.0 not in pct, "把 14.17% 的 14 吃进前一个数后，会凭空多出一个 17%"
    assert "1,239,720,956.43" in money
    assert "1,064,066,560.88" in money


def test_元每股保留四位小数():
    page = _page("基本每股收益（元/股）0.07180.17910.1791")
    vals = [t.value for t in dr.tokens_near(page, ["基本每股收益"], base_unit="元/股")]
    assert vals[:2] == [0.0718, 0.1791]


def test_同值同单位只出现一次():
    page = _page("毛利率（%）\n14.17%\n毛利率比上年同期增减\n14.17%")
    pct = [t.value for t in dr.tokens_near(page, ["毛利率"], base_unit="%")]
    assert pct.count(14.17) == 1


def test_年份不被当成数值():
    page = _page("营业收入2023年1,243,188,170.21元")
    vals = [t.value for t in dr.tokens_near(page, ["营业收入"], base_unit="元")]
    assert 2023 not in vals
    assert 1243188170.21 in vals


# --- 单位与精度 -----------------------------------------------------------

def test_单位换算():
    assert dr.to_base(2.87, "亿元", "元") == pytest.approx(2.87e8)
    assert dr.to_base(1385610.54, "万元", "元") == pytest.approx(1.38561054e10)
    assert dr.to_base(15, "%", "%") == pytest.approx(15)


def test_元与元每股互认():
    """草稿写「每股收益 0.72 元」、材料写「元/股」，是同一个量纲，不算单位错。"""
    assert dr.to_base(0.72, "元", "元/股") == pytest.approx(0.72)
    assert dr.to_base(0.0718, "元/股", "元/股") == pytest.approx(0.0718)


def test_无法换算的单位返回None():
    assert dr.to_base(408548455, "元", "股") is None


def test_按精度截断小数():
    assert dr._truncate_decimals("1,243,188,170.211", 2) == "1,243,188,170.21"
    assert dr._truncate_decimals("0.0718", 4) == "0.0718"
    assert dr._truncate_decimals("408548455", 0) == "408548455"
    assert dr._truncate_decimals("-19.435", 2) == "-19.43"


# --- 证据引用 -------------------------------------------------------------

def test_引用取数值所在行而不是表头():
    page = _page("毛利率\n营业收入比上年同期增减\n营业成本比上年同期增减\n14.17% \n-19.43% ")
    q = dr.snippet(page, "毛利率", value_raw="14.17")
    assert "14.17%" in q
    assert "毛利率" not in q, "取到表头说明没有按数值定位"


def test_竖切数值能拼回来():
    """PDF 会把一个数竖切成两行（`14.1` / `7%`），引用要能跨行找到。"""
    page = _page("毛利率\n营业收入比上\n年同期增减\n14.1\n7%\n-19.43%")
    assert "14.1" in dr.snippet(page, "毛利率", value_raw="14.17%")


# --- 评测匹配 -------------------------------------------------------------

def test_评测按指标加数值匹配而不是按条号():
    """拆解方式不同会导致条号错位，匹配必须按「指标 + 数值」且换单位后可比。"""
    result = dr.DraftReviewResult(
        run_log_id="RUN-x", case_id="c", company="co", cutoff="2024-04-22",
        claims=[
            {"claim_id": "DRAFT-C001", "metric": "营业收入", "metric_key": "revenue",
             "claim_value": 12.43, "claim_unit": "亿元", "cited_page": None,
             "verdict": {"label": "SUPPORTED", "error_type": None}},
        ],
        excluded=[], warnings=[], stages={},
    )
    expected = {
        "claims": [
            {"claim_id": "QMD-001", "metric": "revenue", "value": "1,243,188,170.21",
             "unit": "元", "expected_verdict": "SUPPORTED", "expected_flag_as_error": False},
        ]
    }
    path = ROOT / "out" / "_test_expected.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(expected, ensure_ascii=False), encoding="utf-8")
    try:
        metrics = dr.evaluate(result, path)
    finally:
        path.unlink(missing_ok=True)
    assert metrics["matched_claims"] == 1
    assert metrics["label_accuracy"] == 1.0
    assert metrics["error_detection"]["tp"] == 0
    assert metrics["error_detection"]["fp"] == 0


# --- 端到端（需要本地年报 PDF，未入库） -----------------------------------

needs_pdf = pytest.mark.skipif(
    not (ANNUAL_REPORT.exists() and CASE_PATH.exists()),
    reason="需要本地年报 PDF（data/raw 不入库）",
)


@needs_pdf
def test_端到端判定与错误类型全对():
    payload = json.loads(CASE_PATH.read_text(encoding="utf-8"))
    res = dr.run_draft_case(payload, use_llm=False)
    metrics = dr.evaluate(res, ROOT / payload["expected"])
    assert metrics["matched_claims"] == metrics["gold_claims"] == 13
    assert metrics["label_accuracy"] == 1.0
    det = metrics["error_detection"]
    assert (det["tp"], det["fp"], det["fn"]) == (6, 0, 0)
    assert det["precision"] == det["recall"] == 1.0


@needs_pdf
def test_端到端截止日硬门挡住更正公告():
    payload = json.loads(CASE_PATH.read_text(encoding="utf-8"))
    res = dr.run_draft_case(payload, use_llm=False)
    assert res.stages["stage2_materials"]["allowed"] == ["01_改错前_2023年年度报告.pdf"]
    assert "02_官方更正公告.pdf" in res.stages["stage2_materials"]["excluded"]


@needs_pdf
def test_端到端页码不许编造():
    """主张引了材料里不存在的页码时，输出必须落在「不足以判断」，不得编造页面。"""
    payload = json.loads(CASE_PATH.read_text(encoding="utf-8"))
    res = dr.run_draft_case(payload, use_llm=False)
    for claim in res.claims:
        if claim["verdict"]["label"] == "INSUFFICIENT":
            assert not claim["verdict"]["evidence"], "证据不足的主张不得携带正面证据"
