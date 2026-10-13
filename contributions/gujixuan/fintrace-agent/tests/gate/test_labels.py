"""模块 F 必测（一） —— gate/labels.py 的映射口径（v2.2 §2.6）。"""
from __future__ import annotations

import pytest

from fintrace_agent.contracts import LABEL_MAP, ReportLabel, Verdict
from fintrace_agent.gate.labels import report_labels


def test_contradicted_gets_two_labels():
    """CONTRADICTED → 风险信号 + 建议进一步核查（且顺序固定）。"""
    labels = report_labels(Verdict.CONTRADICTED.value)
    assert labels == [
        ReportLabel.RISK_SIGNAL_DETECTED.value,
        ReportLabel.FURTHER_VERIFICATION_SUGGESTED.value,
    ]


def test_supported_produces_no_risk_label():
    """SUPPORTED 不产生任何风险标签 —— 留在 findings 内部。"""
    assert report_labels(Verdict.SUPPORTED.value) == []


def test_prediction_produces_no_risk_label():
    assert report_labels(Verdict.PREDICTION.value) == []


def test_insufficient_maps_to_insufficient_information():
    assert report_labels(Verdict.INSUFFICIENT.value) == [
        ReportLabel.INSUFFICIENT_INFORMATION.value
    ]


def test_confirmed_label_only_in_post_hoc_with_authoritative_doc():
    """只有「有正式确认文件」且 post_hoc 才给 CONFIRMED_BY_AUTHORITATIVE_DOCUMENT。"""
    confirmed = ReportLabel.CONFIRMED_BY_AUTHORITATIVE_DOCUMENT.value

    # 事后验证 + 有确认文件 → 给
    got = report_labels(Verdict.SUPPORTED.value, authoritative_doc=True, mode="post_hoc")
    assert got == [confirmed]

    # 历史回放（pre_hoc）+ 有确认文件 → 不给（时间硬约束）
    assert report_labels(Verdict.SUPPORTED.value, authoritative_doc=True, mode="pre_hoc") == []

    # post_hoc 但没有确认文件 → 不给
    assert confirmed not in report_labels(
        Verdict.CONTRADICTED.value, authoritative_doc=False, mode="post_hoc")


def test_contradicted_plus_confirmed_keeps_both():
    labels = report_labels(
        Verdict.CONTRADICTED.value, authoritative_doc=True, mode="post_hoc")
    assert ReportLabel.RISK_SIGNAL_DETECTED.value in labels
    assert ReportLabel.FURTHER_VERIFICATION_SUGGESTED.value in labels
    assert ReportLabel.CONFIRMED_BY_AUTHORITATIVE_DOCUMENT.value in labels


def test_mapping_matches_contract_label_map():
    """逐项与 contracts.LABEL_MAP 对表：映射只有一处，不许各写一套。"""
    for v in Verdict:
        mapped = LABEL_MAP[v]
        base = report_labels(v.value)
        expected = [mapped.value] if mapped is not None else []
        assert base[:len(expected)] == expected
        if v is Verdict.CONTRADICTED:
            assert ReportLabel.FURTHER_VERIFICATION_SUGGESTED.value in base
        else:
            assert ReportLabel.FURTHER_VERIFICATION_SUGGESTED.value not in base


def test_unknown_verdict_raises():
    with pytest.raises(ValueError):
        report_labels("NOT_A_VERDICT")
