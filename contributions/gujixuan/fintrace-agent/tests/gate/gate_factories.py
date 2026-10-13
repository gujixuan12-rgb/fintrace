"""tests/gate 的样例构造器。

为什么单独一个**带目录前缀**的模块：多目录套件里 pytest 会把每个测试目录都插进 sys.path，
裸名导入（`from conftest import ...`，甚至 `from factories import ...`，只要两个目录同名）
会命中最先被导入的那个，表现为「全量能过、换个顺序或单跑某目录就 ImportError」。
模块名全局唯一，导入结果就与收集顺序无关。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 兜底：即使没走 pyproject 的 pythonpath 也能 import
_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from fintrace_agent.contracts import (  # noqa: E402
    Assertion,
    Bounds,
    Claim,
    Finding,
    TaskState,
)


#: 假原件：file_name -> {page: 文本}
FILE = "2023年年度报告.pdf"
PAGE = 12
PAGE_TEXT = (
    "第五节 重要事项。2023年年度现金分红金额为27,116,214.63元。"
    "营业收入 4,202,030,347.42 元。"
)
VALUE = "27116214.63"
RAW = "27,116,214.63"
QUOTE = "2023年年度现金分红金额为27,116,214.63元"


def make_page_texts(pages: dict | None = None):
    """返回 ``page_texts(file_name, page)`` 假函数；未登记的组合返回 None。"""
    table = pages if pages is not None else {(FILE, PAGE): PAGE_TEXT}

    def page_texts(file_name, page):
        return table.get((file_name, page))

    return page_texts


def make_evidence_dict(
    *,
    file_name: str = FILE,
    page: int | None = PAGE,
    value=RAW,
    quote: str = QUOTE,
    assertion_id: str = "",
    **extra,
) -> dict:
    """造一条 Finding.evidence 条目（贴近引擎 ``_ev`` 的字段形状）。"""
    ev = {
        "file_name": file_name,
        "page": page,
        "value": value,
        "raw": RAW if value == RAW else str(value),
        "quote": quote,
    }
    if assertion_id:
        ev["assertion_id"] = assertion_id
    ev.update(extra)
    return ev


def make_finding(
    *,
    finding_id: str = "F1",
    claim_id: str = "C1",
    verdict: str = "CONTRADICTED",
    evidence: list | None = None,
    audit: dict | None = None,
    correction: dict | None = None,
) -> Finding:
    if evidence is None:
        evidence = [make_evidence_dict()]
    return Finding(
        finding_id=finding_id,
        claim_id=claim_id,
        verdict=verdict,
        basis="复算与披露不一致",
        suggested_fix="核对分红金额",
        evidence=evidence,
        citation_pages=[PAGE],
        correction=correction or {},
        audit=audit or {},
    )


def make_assertion(
    assertion_id: str = "A1",
    *,
    evidence_status: str = "attested",
    semantic_status: str = "bound",
) -> Assertion:
    """默认造一条「事实」（双轴都过）。"""
    return Assertion(
        assertion_id=assertion_id,
        kind="financial_value",
        subject="启明信息",
        period="2023A",
        metric="现金分红金额",
        metric_id="cash_dividend_amount",
        value="27116214.63",
        unit="元",
        caliber="合并",
        raw_text=RAW,
        decimals_as_disclosed=2,
        origin="material",
        evidence_status=evidence_status,
        semantic_status=semantic_status,
        run_id="FT-20261010-aaaaaaaa",
    )


def make_claim(
    claim_id: str = "C1",
    *,
    status: str = "verified",
    open_questions: list | None = None,
    evidence_ids: list | None = None,
) -> Claim:
    return Claim(
        claim_id=claim_id,
        claim_text="2023年现金分红金额与披露一致",
        status=status,
        open_questions=open_questions or [],
        evidence_ids=evidence_ids or [],
    )


def make_state(
    *,
    claims: list | None = None,
    steps: list | None = None,
    unresolved: list | None = None,
    allowed_tools: list | None = None,
) -> TaskState:
    bounds = Bounds(cutoff_date="2024-04-30")
    if allowed_tools is not None:
        bounds.allowed_tools = list(allowed_tools)
    return TaskState(
        run_id="FT-20261010-aaaaaaaa",
        mode="agent",
        goal={"case_id": "QM-DRAFT-2023-001"},
        bounds=bounds,
        claims=claims or [],
        steps=steps or [],
        unresolved=unresolved or [],
    )
