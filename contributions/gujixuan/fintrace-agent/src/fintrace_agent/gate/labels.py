"""报告标签映射（判定四类 → 对外五类）。

唯一映射处是 ``contracts.LABEL_MAP``（v2.2 §2.6）；本模块只负责：

* 把判定（内部四类：``SUPPORTED / CONTRADICTED / INSUFFICIENT / PREDICTION``）
  翻成报告标签（对外五类）；
* ``CONTRADICTED`` 额外给一条 ``FURTHER_VERIFICATION_SUGGESTED``；
* 仅在「有监管/正式确认文件」**且**任务模式为 ``post_hoc`` 时，补一条
  ``CONFIRMED_BY_AUTHORITATIVE_DOCUMENT``。

不变量：

* ``SUPPORTED`` 与 ``PREDICTION`` **不产生任何风险标签**（返回空列表）；
* 未 ``post_hoc``（历史回放 ``pre_hoc``）**绝不**产出 ``CONFIRMED_BY_AUTHORITATIVE_DOCUMENT``；
* 只用标准库。

注意：这里只管「标签怎么出」，不管「标签能不能发」。发布与否由 V 门
（``finding_gate.FindingGate``）逐条裁决。
"""
from __future__ import annotations

from ..contracts import LABEL_MAP, ReportLabel, Verdict

__all__ = ["report_labels"]

#: 需要附带「建议进一步核查」的判定：目前只有矛盾类。
_ALSO_FURTHER_VERIFICATION: frozenset[Verdict] = frozenset({Verdict.CONTRADICTED})

#: 只有在「事后验证」模式下，监管/正式确认文件才具备标注资格。
_POST_HOC_MODE: str = "post_hoc"


def report_labels(
    verdict: str,
    *,
    authoritative_doc: bool = False,
    mode: str = "pre_hoc",
) -> list[str]:
    """把判定翻成报告标签列表。

    参数
    ----
    verdict:
        内部判定，取值须是 :class:`~fintrace_agent.contracts.Verdict` 之一
        （``SUPPORTED`` / ``CONTRADICTED`` / ``INSUFFICIENT`` / ``PREDICTION``）。
    authoritative_doc:
        本条结论是否由监管或公司正式确认文件支撑（如问询函回复中的认定结论、
        更正公告）。默认 ``False``。
    mode:
        ``"pre_hoc"``（历史回放，默认）或 ``"post_hoc"``（事后验证）。

    返回
    ----
    报告标签字符串列表；可能为空（``SUPPORTED`` / ``PREDICTION`` 且无权威确认）。
    """
    try:
        v = Verdict(verdict)
    except ValueError as exc:
        raise ValueError(
            f"未知判定 {verdict!r}；只能是 {[x.value for x in Verdict]}"
        ) from exc

    labels: list[str] = []

    mapped = LABEL_MAP.get(v)
    if mapped is not None:
        labels.append(mapped.value)
        if v in _ALSO_FURTHER_VERIFICATION:
            labels.append(ReportLabel.FURTHER_VERIFICATION_SUGGESTED.value)

    # 权威确认标签：只在「有确认文件」且「事后验证模式」下才出现。
    if authoritative_doc and mode == _POST_HOC_MODE:
        labels.append(ReportLabel.CONFIRMED_BY_AUTHORITATIVE_DOCUMENT.value)

    # 去重但保序（理论上不会重复，防御性处理）。
    out: list[str] = []
    for label in labels:
        if label not in out:
            out.append(label)
    return out
