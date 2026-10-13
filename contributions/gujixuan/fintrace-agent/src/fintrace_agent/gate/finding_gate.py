"""最终核验门 V —— 逐条 Finding 的确定性发布裁决（V1~V7，v2.2 §7）。

一句话：**W 管「数据能不能进」，V 管「结论能不能出」。**

本模块只做 V 门，**不重造** W 门与语义绑定：它只读它们的产出 ——
引用是不是 ``Assertion.is_fact``（``evidence_status == attested`` 且
``semantic_status == bound``）、``Relation.applicable`` 是否成立。

检查项（BUILD_SPEC §6）：

* **V1 结构**：``finding_id`` / ``claim_id`` / ``verdict`` 齐全且判定合法；
* **V2 回查原件**：对每条引用用 ``page_texts(file_name, page)`` 取原文，
  值/引文对不上即 ``blocked``；取不到原文 → ``manual_review`` 并记一条 residual；
* **V3 关系适用性**：引用的 ``Relation`` 必须 ``applicable``，否则 ``blocked``；
  适用条件未知 → ``manual_review``；
* **V4 Task State**：未处理关键疑点、失败工具调用、超授权、未完成步骤 → 记 residual；
* **V5 语义绑定**：引用的 ``Assertion`` 必须 ``is_fact``，否则不得 ``allowed``
  （未绑定 → ``manual_review``）；
* **V6 残余清单**：只要有 ``residuals`` 就必须出现在结果里；
* **V7 发布决定**：任一阻断 → ``blocked``；任一待复核 → ``manual_review``；否则 ``allowed``。

引用从哪读（找不到专门字段，故约定以下键；契约不改）：

* 断言的引用：``Finding.audit`` / ``Finding.correction`` 里的
  ``assertions`` / ``assertion_ids`` / ``evidence_ids``，``Finding.evidence[*].assertion_id``，
  以及同 ``claim_id`` 的 ``Claim.evidence_ids``；
* 关系的引用：``Finding.audit`` / ``Finding.correction`` 里的 ``relations``
  （``Relation`` 对象或 ``Relation.to_dict()`` 或 ``relation_id``），
  亦可放在 ``Finding.evidence[*].relations``。

``verifier``（按需审查模型）只能**拉低**，绝不能把阻断抬成放行 —— 与语义绑定同一条
权限不对称原则（v2.2 §7）。

只用标准库。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

from ..contracts import (
    Assertion,
    Finding,
    Publish,
    Relation,
    Residual,
    Verdict,
)
from .labels import report_labels

__all__ = ["FindingGate", "GateResult"]

#: 取原文的注入口：``(file_name, page) -> str | None``。取不到返回 None。
PageTexts = Callable[[str, Optional[int]], Optional[str]]

#: 需要「引用存在」的判定：这两类必须带证据，否则 V2 直接阻断。
_CITATION_REQUIRED: frozenset[str] = frozenset(
    {Verdict.CONTRADICTED.value, Verdict.SUPPORTED.value}
)

#: 判定「纯数字串」用（用于数值的按位归属判定，避免子串误命中）。
_NUMERIC_RX = re.compile(r"^\d+(?:\.\d+)?$")

#: 去掉千分位逗号、各类空白（含全角空格）后再比对。
_NUM_JUNK_RX = re.compile(r"[\s\u3000,]")

#: 步骤级别的失败 / 未完成状态词。
_FAILED_STATUS = frozenset({"failed", "error", "failure"})
_UNFINISHED_STATUS = frozenset({"pending", "running", "in_progress", "open", "unfinished"})

#: Finding.audit / correction 里可能承载引用列表的键。
#: 引文切分与片段识别：引擎的引文用「/」「|」分隔单元格，还常带省略号
_QUOTE_SPLIT = re.compile(r"[\/|｜\n]+")
_QUOTE_TRUNC = re.compile(r"…|\.\.\.|\u2026")
_QUOTE_TRIVIAL = re.compile(r"^[\s（）()【】\[\]元股%％,，.。:：、·—\-…]+$")

_ASSERTION_KEYS = ("assertions", "assertion_ids", "evidence_ids")
_RELATION_KEYS = ("relations", "relation_refs", "relation")


def _flat(text: str) -> str:
    """去掉全部空白，得到用于比对的无空串。"""
    return re.sub(r"\s+", "", text or "")


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _contains(text: str, token: Any) -> bool:
    """``token`` 是否出现在 ``text`` 里。

    * 纯数字 token：去掉千分位/空白后按「数字边界」匹配，避免 ``100`` 误命中 ``1000``；
    * 其它 token：去空白后按子串匹配。
    """
    if token is None:
        return False
    tok = str(token).strip()
    if not tok:
        return False
    ftok = _flat(tok)
    if not ftok:
        return False
    numeric = _NUM_JUNK_RX.sub("", ftok)
    if _NUMERIC_RX.match(numeric):
        hay = _NUM_JUNK_RX.sub("", _flat(text))
        pattern = r"(?<![\d.])" + re.escape(numeric) + r"(?![\d])"
        return re.search(pattern, hay) is not None
    return ftok in _flat(text)


def _dedupe(items: Iterable[str]) -> list[str]:
    out: list[str] = []
    for it in items:
        if it not in out:
            out.append(it)
    return out


@dataclass
class GateResult:
    """V 门的产出：已填好 audit / publish / block_reasons / report_labels 的 Finding。"""

    findings: list[Finding]
    residuals: list[Residual]
    counts: dict

    def to_dict(self) -> dict:
        return {
            "findings": [f.to_dict() for f in self.findings],
            "residuals": [r.to_dict() for r in self.residuals],
            "counts": dict(self.counts),
        }


class FindingGate:
    """确定性发布裁决器。构造后对一批 ``Finding`` 调用 :meth:`review`。"""

    def __init__(
        self,
        *,
        evidence,
        kb,
        state,
        page_texts: PageTexts,
        assertions_by_id: dict,
        verifier: Optional[Callable[[dict], dict]] = None,
        mode: str = "pre_hoc",
        require_refs: bool = False,
    ) -> None:
        #: 证据日志（EvidenceLog）。可为 None；用于回查被引用的 Assertion。
        self.evidence = evidence
        #: 领域知识库（KnowledgeBase）。可为 None；用于关系规则查证。
        self.kb = kb
        #: 任务状态（TaskState）。V4 的输入。
        self.state = state
        #: 取原文的注入口：``(file_name, page) -> str | None``。
        self.page_texts = page_texts
        #: ``assertion_id -> Assertion`` 的查找表。
        self.assertions_by_id = assertions_by_id or {}
        #: 按需审查模型；只能拉低，不能放行。
        self.verifier = verifier
        #: 任务模式：``pre_hoc`` / ``post_hoc``。
        self.mode = mode
        #: 是否要求每条 Finding 显式引用断言：agent 模式置 True；
        #: fixed 基线（引擎直出、没有两轴模型）置 False，否则会把整批基线结论都打成待复核。
        self.require_refs = require_refs

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def review(self, findings: list[Finding]) -> GateResult:
        """逐条裁决；顺带汇总 run 级残余问题（V6）。"""
        residuals: list[Residual] = []
        # V4：先收 run 级残余（未处理疑点 / 失败工具 / 超授权 / 未完成步骤）。
        residuals.extend(self._state_residuals())

        out: list[Finding] = []
        for finding in findings or []:
            out.append(self._review_one(finding, residuals))

        residuals = _dedupe_residuals(residuals)
        counts = {
            Publish.ALLOWED.value: 0,
            Publish.BLOCKED.value: 0,
            Publish.MANUAL_REVIEW.value: 0,
        }
        for f in out:
            counts[f.publish] = counts.get(f.publish, 0) + 1
        return GateResult(findings=out, residuals=residuals, counts=counts)

    # ------------------------------------------------------------------
    # 单条 Finding
    # ------------------------------------------------------------------
    def _review_one(self, finding: Finding, residuals: list[Residual]) -> Finding:
        block: list[str] = []
        manual: list[str] = []
        v_checks: dict[str, Any] = {}

        # V1 结构
        v1 = self._check_v1(finding)
        v_checks["V1"] = v1
        if not v1["ok"]:
            block.extend(v1["reasons"])

        # V2 回查原件
        v2 = self._check_v2(finding, residuals)
        v_checks["V2"] = v2
        block.extend(v2["blocked"])
        manual.extend(v2["manual"])

        # V3 关系适用性
        v3 = self._check_v3(finding)
        v_checks["V3"] = v3
        block.extend(v3["blocked"])
        manual.extend(v3["manual"])

        # V5 语义绑定
        v5 = self._check_v5(finding)
        v_checks["V5"] = v5
        block.extend(v5["blocked"])
        manual.extend(v5["manual"])

        block = _dedupe(block)
        manual = _dedupe(manual)

        # 审查模型（可选）：只在确定性检查全过时调用，且只能拉低，不能放行。
        if self.verifier is not None and not block:
            reason = self._run_verifier(finding)
            if reason:
                manual.append(reason)

        # V7 发布决定
        if block:
            finding.publish = Publish.BLOCKED.value
        elif manual:
            finding.publish = Publish.MANUAL_REVIEW.value
        else:
            finding.publish = Publish.ALLOWED.value

        # 标签：判定合法才翻；非法判定在 V1 已阻断。
        try:
            finding.report_labels = report_labels(
                finding.verdict,
                authoritative_doc=bool((finding.audit or {}).get("authoritative_doc")),
                mode=self.mode,
            )
        except ValueError:
            finding.report_labels = []

        finding.audit = dict(finding.audit or {})
        finding.audit["v_checks"] = v_checks
        finding.audit["v_manual"] = manual
        finding.block_reasons = block + manual
        return finding

    # ------------------------------------------------------------------
    # V1 结构
    # ------------------------------------------------------------------
    @staticmethod
    def _check_v1(finding: Finding) -> dict:
        reasons: list[str] = []
        if not (finding.finding_id or "").strip():
            reasons.append("V1: finding_id 缺失")
        if not (finding.claim_id or "").strip():
            reasons.append("V1: claim_id 缺失")
        try:
            Verdict(finding.verdict)
        except (ValueError, TypeError):
            reasons.append(f"V1: 判定非法 {finding.verdict!r}")
        return {"ok": not reasons, "reasons": reasons}

    # ------------------------------------------------------------------
    # V2 回查原件
    # ------------------------------------------------------------------
    def _check_v2(self, finding: Finding, residuals: list[Residual]) -> dict:
        blocked: list[str] = []
        manual: list[str] = []
        notes: list[str] = []          # 非阻断提示：引文是片段之类，只记不拦
        cites = [e for e in (finding.evidence or []) if isinstance(e, dict)]

        # 「引用存在」是 V2 的一部分：矛盾/支撑类判定必须带引用。
        if finding.verdict in _CITATION_REQUIRED and not cites:
            blocked.append("V2: 该判定必须带证据引用，但 Finding 无任何引用")
            return {"blocked": blocked, "manual": manual, "notes": [], "n": 0}

        for i, ev in enumerate(cites):
            file_name = ev.get("file_name") or ev.get("file") or ""
            page = ev.get("page")
            if not file_name or page is None:
                manual.append(f"V2: 第{i}条引用缺文件名或页码，无法回查原件")
                residuals.append(Residual(
                    claim_id=finding.claim_id, kind="blocked",
                    text=f"引用缺定位信息（file_name={file_name!r}, page={page!r}）",
                    reason="无法回查原文"))
                continue

            text = self._page_text(file_name, page)
            if text is None:
                manual.append(f"V2: 取不到原件（{file_name} 第 {page} 页），该条需人工复核")
                residuals.append(Residual(
                    claim_id=finding.claim_id, kind="blocked",
                    text=f"{file_name} 第 {page} 页原文不可得",
                    reason="取不到原文"))
                continue

            value = ev.get("value")
            raw = ev.get("raw")
            quote = ev.get("quote")

            tokens = [t for t in (value, raw) if t not in (None, "")]
            value_ok: Optional[bool] = None
            if tokens:
                value_ok = any(self._value_in_page(text, t) for t in tokens)
                if not value_ok:
                    blocked.append(
                        f"V2: 值与原件对不上（{file_name} 第 {page} 页找不到 {value!r}）")

            if quote:
                ok, why, fragment = self._quote_ok(text, quote)
                if not ok:
                    blocked.append(
                        f"V2: 引文与原件对不上（{file_name} 第 {page} 页）：{why}")
                elif fragment:
                    notes.append(
                        f"V2: 第{i}条引文是截断片段（{file_name} 第 {page} 页），"
                        f"已按片段逐段回查命中")
            if not tokens and not quote:
                manual.append(f"V2: 第{i}条引用既无值也无引文，无法回查")

        return {"blocked": _dedupe(blocked), "manual": _dedupe(manual),
                "notes": _dedupe(notes), "n": len(cites)}

    def _quote_ok(self, text: str, quote: Any) -> tuple[bool, str, bool]:
        """引文回查：**按片段逐段核对**，返回 (是否通过, 失败原因, 是否片段形态)。

        为什么不是整句子串匹配：引擎给出的引文本来就是单元格/行片段，
        形如「营业收入 / （元） / 1,243,18」，整句在原文里当然找不到；
        拿它做字面子串匹配会把正常结论整批拦成 blocked（集成时真的发生过，
        15 条里拦了 14 条）。文档口径不变 —— 引文对不上仍然阻断 ——
        只是把「对不上」的判据改成「每个实质片段都能在原件里找到」。
        """
        raw = str(quote or "")
        parts = [p.strip() for p in _QUOTE_SPLIT.split(raw)]
        substantive = [p for p in parts if p and not _QUOTE_TRIVIAL.match(p)]
        fragment = bool(len(parts) > 1 or _QUOTE_TRUNC.search(raw))
        if not substantive:
            return True, "", fragment
        missing = [p for p in substantive
                   if not (self._value_in_page(text, p) or _contains(text, p))]
        if missing:
            return False, "引文片段在原件里找不到：" + "、".join(missing[:3]), fragment
        return True, "", fragment

    @staticmethod
    def _value_in_page(text: str, token: Any) -> bool:
        """值是否在原件里出现 —— 复用引擎同款口径（与基线原件回查一致）。"""
        try:
            from fintrace_engine import draft_review as dr

            if dr.num_in_text(text, str(token)):
                return True
        except Exception:  # noqa: BLE001 —— 引擎不可用时退回本模块的宽松比较
            pass
        return _contains(text, token)

    def _page_text(self, file_name: str, page: Any) -> Optional[str]:
        """取原文；调用方注入的 ``page_texts`` 抛异常时按「取不到」处理。"""
        try:
            text = self.page_texts(file_name, page)
        except Exception:  # noqa: BLE001 —— 取原文失败一律按取不到处理
            return None
        return text if isinstance(text, str) else (None if text is None else str(text))

    # ------------------------------------------------------------------
    # V3 关系适用性
    # ------------------------------------------------------------------
    def _check_v3(self, finding: Finding) -> dict:
        blocked: list[str] = []
        manual: list[str] = []
        for ref in self._cited_relations(finding):
            rel = self._resolve_relation(ref)
            if rel is None:
                # 只给了 rule_id / relation_id：没有适用条件，无法判适用性。
                hint = ""
                rid = ref if isinstance(ref, str) else ""
                if rid and self.kb is not None:
                    rule = None
                    try:
                        rule = self.kb.relation_rule(rid)
                    except Exception:  # noqa: BLE001
                        rule = None
                    if rule:
                        hint = "（规则存在，但未给出适用条件的逐字段结论）"
                manual.append(f"V3: 关系 {ref!r} 未给出适用条件，无法判定适用性{hint}")
                continue
            if rel.applicable:
                continue
            failed = rel.failed_fields
            if failed:
                blocked.append(
                    f"V3: 关系 {rel.relation_id} 适用条件不成立（字段 {','.join(failed)}）")
            else:
                manual.append(f"V3: 关系 {rel.relation_id} 适用条件未验证（unknown）")
        return {"blocked": blocked, "manual": manual}

    def _cited_relations(self, finding: Finding) -> list:
        out: list = list(getattr(finding, "relation_refs", None) or [])
        for src in (finding.audit or {}, finding.correction or {}):
            if isinstance(src, dict):
                for key in _RELATION_KEYS:
                    if key in src:
                        out.extend(_as_list(src[key]))
        for ev in (finding.evidence or []):
            if isinstance(ev, dict):
                for key in ("relations", "relation_refs"):
                    if key in ev:
                        out.extend(_as_list(ev[key]))
        return out

    @staticmethod
    def _resolve_relation(ref: Any) -> Optional[Relation]:
        if isinstance(ref, Relation):
            return ref
        if isinstance(ref, dict):
            try:
                return Relation.from_dict(ref)
            except Exception:  # noqa: BLE001
                return None
        return None

    # ------------------------------------------------------------------
    # V5 语义绑定（只读产出，不重造绑定）
    # ------------------------------------------------------------------
    def _check_v5(self, finding: Finding) -> dict:
        blocked: list[str] = []
        manual: list[str] = []
        for cand in self._cited_assertions(finding):
            a = self._resolve_assertion(cand)
            if a is None:
                manual.append(f"V5: 引用断言 {_ref_label(cand)} 不存在或不可解析")
                continue
            if not a.is_fact:
                manual.append(
                    f"V5: 引用断言 {a.assertion_id} 未通过双轴"
                    f"（evidence={a.evidence_status}, semantic={a.semantic_status}）")
        # 堵漏：Finding 声称有材料支撑（证据里带值或原文）却一个断言都不引，
        # 两轴就无从核起 —— 这时候放行等于没检查。require_refs=True 时降级为待人工复核。
        if getattr(self, "require_refs", False) and not self._cited_assertions(finding):
            if self._claims_material_backing(finding):
                manual.append("V5: 引用了材料值但未引用任何已核验断言，双轴无从核起")
        return {"blocked": blocked, "manual": _dedupe(manual)}

    @staticmethod
    def _claims_material_backing(finding: Finding) -> bool:
        """这条 Finding 是否自称有材料支撑：证据里有值 / 原文 / 页码即算。"""
        for ev in (finding.evidence or []):
            if not isinstance(ev, dict):
                continue
            if any(k in ev and ev.get(k) not in (None, "") for k in ("value", "raw", "quote", "quoted_text")):
                return True
        return False

    def _cited_assertions(self, finding: Finding) -> list:
        out: list = list(getattr(finding, "assertion_refs", None) or [])
        for src in (finding.audit or {}, finding.correction or {}):
            if isinstance(src, dict):
                for key in _ASSERTION_KEYS:
                    if key in src:
                        out.extend(_as_list(src[key]))
        for ev in (finding.evidence or []):
            if isinstance(ev, dict):
                if ev.get("assertion") is not None:
                    out.append(ev["assertion"])
                elif ev.get("assertion_id"):
                    out.append(ev["assertion_id"])
        # 同 claim 的主张上登记的 evidence_ids 也算被引用。
        if self.state is not None:
            for claim in getattr(self.state, "claims", []) or []:
                if claim.claim_id == finding.claim_id:
                    out.extend(claim.evidence_ids or [])
        return out

    def _resolve_assertion(self, cand: Any) -> Optional[Assertion]:
        if isinstance(cand, Assertion):
            return cand
        if isinstance(cand, dict):
            try:
                return Assertion.from_dict(cand)
            except Exception:  # noqa: BLE001
                return None
        if isinstance(cand, str):
            if cand in self.assertions_by_id:
                return self.assertions_by_id[cand]
            if self.evidence is not None:
                try:
                    return self.evidence.assertion(cand)
                except Exception:  # noqa: BLE001
                    return None
        return None

    # ------------------------------------------------------------------
    # V4 Task State
    # ------------------------------------------------------------------
    def _state_residuals(self) -> list[Residual]:
        out: list[Residual] = []
        st = self.state
        if st is None:
            return out

        # 既有未解决项原样带出。
        for r in getattr(st, "unresolved", []) or []:
            out.append(r if isinstance(r, Residual) else Residual(**r))

        # 未处理主张 + 未解疑点。
        for claim in getattr(st, "open_claims", []) or []:
            out.append(Residual(
                claim_id=claim.claim_id, kind="unresolved_claim",
                text=claim.claim_text or "", reason=f"主张状态仍为 {claim.status}"))
            for q in (claim.open_questions or []):
                out.append(Residual(
                    claim_id=claim.claim_id, kind="open_question", text=str(q),
                    reason="关键疑点未处理"))

        allowed = set(getattr(getattr(st, "bounds", None), "allowed_tools", []) or [])
        for i, step in enumerate(getattr(st, "steps", []) or []):
            if not isinstance(step, dict):
                continue
            action = step.get("action") or step.get("tool") or ""
            if _step_failed(step):
                out.append(Residual(
                    claim_id=step.get("claim_id", ""), kind="failed_tool",
                    text=f"步骤{i} {action} 失败：{step.get('error') or step.get('status')}",
                    reason="失败的工具调用"))
            if action.startswith("t_") and allowed and action not in allowed:
                out.append(Residual(
                    claim_id=step.get("claim_id", ""), kind="unauthorized",
                    text=f"步骤{i} 动作 {action} 不在本 run 授权集合内",
                    reason="超授权操作"))
            if action and _step_unfinished(step):
                out.append(Residual(
                    claim_id=step.get("claim_id", ""), kind="unfinished_step",
                    text=f"步骤{i} {action} 未完成（status={step.get('status')})",
                    reason="未完成的核查步骤"))
        return out

    # ------------------------------------------------------------------
    # 审查模型
    # ------------------------------------------------------------------
    def _run_verifier(self, finding: Finding) -> str:
        """调用审查模型；异常也按「有疑点」处理（宁可复核，不放行）。"""
        try:
            res = self.verifier(finding.to_dict())
        except Exception as exc:  # noqa: BLE001
            return f"V7(r): 审查模型调用失败：{exc}"
        if isinstance(res, dict) and res.get("ok") is False:
            reason = res.get("reason") or res.get("code") or "未说明"
            return f"V7(r): 审查模型提出疑点：{reason}"
        return ""


def _step_failed(step: dict) -> bool:
    if step.get("ok") is False:
        return True
    if step.get("status") in _FAILED_STATUS:
        return True
    return bool(step.get("error"))


def _step_unfinished(step: dict) -> bool:
    return step.get("status") in _UNFINISHED_STATUS


def _ref_label(cand: Any) -> str:
    if isinstance(cand, dict):
        return str(cand.get("assertion_id") or "<dict>")
    return str(cand)


def _dedupe_residuals(items: list[Residual]) -> list[Residual]:
    seen: set[tuple] = set()
    out: list[Residual] = []
    for r in items:
        key = (r.claim_id, r.kind, r.text, r.reason)
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out
