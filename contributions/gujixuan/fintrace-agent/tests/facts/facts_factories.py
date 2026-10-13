"""tests/facts 的样例构造器。

为什么单独一个模块而不是放在 conftest.py：多目录套件里 pytest 会把每个测试目录都插进
sys.path，`from conftest import ...` 这种裸名导入会命中**先被导入的那个** conftest
（取决于收集顺序）—— 表现为「全量跑能过、单跑某个目录就 ImportError」。
把构造器放在名字唯一的本模块里，导入结果就与收集顺序无关。
"""
from __future__ import annotations

from fintrace_agent.contracts import (
    ApplicabilityField,
    Assertion,
    MatchResult,
    Relation,
)


def make_assertion(
    assertion_id: str = "A1",
    *,
    subject: str = "启明信息",
    period: str = "2023A",
    metric: str = "现金分红金额",
    kind: str = "financial_value",
    evidence_status: str = "attested",
    semantic_status: str = "bound",
    run_id: str = "FT-20261010-aaaaaaaa",
) -> Assertion:
    """构造一条断言；默认就是「事实」（双轴都过）。"""
    return Assertion(
        assertion_id=assertion_id,
        kind=kind,
        subject=subject,
        period=period,
        metric=metric,
        metric_id="cash_dividend_amount",
        value="27116214.63",
        unit="元",
        caliber="合并",
        raw_text="27,116,214.63",
        decimals_as_disclosed=2,
        text="2023年年度现金分红金额为27,116,214.63元。",
        source_spans=["S1", "S2"],
        origin="material",
        evidence_status=evidence_status,
        semantic_status=semantic_status,
        run_id=run_id,
    )


def make_relation(
    relation_id: str = "R1",
    *,
    subject_ref: str = "A1",
    object_ref: str = "A2",
    rule_id: str = "frm.cash_dividend_per_share",
    run_id: str = "FT-20261010-aaaaaaaa",
    applicability: dict | None = None,
) -> Relation:
    """构造一条关系；默认适用条件字段全部 verified（applicable=True）。"""
    if applicability is None:
        applicability = {
            "caliber": ApplicabilityField(claim="合并", evidence="合并", match=MatchResult.VERIFIED.value),
            "period": ApplicabilityField(claim="2023A", evidence="2023A", match=MatchResult.VERIFIED.value),
        }
    return Relation(
        relation_id=relation_id,
        predicate="equals",
        subject_ref=subject_ref,
        object_ref=object_ref,
        applicability=applicability,
        rule_id=rule_id,
        rule_version="v1",
        status="verified",
        run_id=run_id,
    )
