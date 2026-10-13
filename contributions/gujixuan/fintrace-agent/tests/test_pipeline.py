"""端到端自检：fixed 全链路 + agent 回路（用假模型，不触网）。

真案件、真 PDF、真原件回查都在这里跑一遍；模型用**测试替身**，所以这条测试离线可复现。
agent 模式真正接上真模型属于联网行为，不在单测里做。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from fintrace_agent import paths as P
from fintrace_agent.contracts import Assertion, Origin

CASE = "QM-DRAFT-2023-001"


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    """运行时产物与事实库都写临时目录，别污染项目目录。"""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("FINTRACE_HOME", str(home))
    return home


def _db_paths(tmp_path: Path) -> dict:
    return {"kb": P.KNOWLEDGE_DB, "facts": tmp_path / "facts.sqlite"}


def test_fixed_case_end_to_end(tmp_path):
    from fintrace_agent.pipeline import run_fixed_case

    out = run_fixed_case(CASE)
    assert out.exit == "fixed"
    assert out.findings, "固定链路必须产出结论"
    assert out.state.mode == "fixed"

    run_dir = Path(out.run_dir)
    for name in ("state.json", "findings.json", "report.md", "decisions.jsonl"):
        assert (run_dir / name).is_file(), f"缺产物 {name}"

    payload = json.loads((run_dir / "findings.json").read_text(encoding="utf-8"))
    assert payload["run_id"] == out.run_id
    assert payload["gate_counts"].get("allowed", 0) >= 1
    assert payload["claims"], "主张状态要落盘（SR 指标要用）"

    md = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "未解决清单" in md and "证据分层" in md and "需经专业人员" in md

    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    assert state["bounds"]["cutoff_date"] == "2024-04-22"
    assert state["bounds"]["as_of_mode"] == "historical_replay"


def test_agent_case_falls_back_without_model(tmp_path):
    """没配模型也必须能跑完 —— 回落基线，并且在产物里写明原因。"""
    from fintrace_agent.pipeline import run_agent_case

    out = run_agent_case(CASE, client=None, db_paths=_db_paths(tmp_path))
    assert out.exit == "fixed"
    assert any("回落" in n for n in out.notes)
    assert out.findings


class FakeClient:
    """测试替身：按剧本返回决策 JSON，不联网。"""

    configured = True

    def __init__(self, actions: list[dict]):
        self.actions = list(actions)
        self.seen_contexts: list[dict] = []

    def decide(self, context: dict):
        self.seen_contexts.append(dict(context))
        idx = min(len(self.seen_contexts) - 1, len(self.actions) - 1)
        return json.dumps(self.actions[idx], ensure_ascii=False), {"total_tokens": 12}


def test_agent_loop_runs_end_to_end_with_fake_model(tmp_path):
    from fintrace_agent.pipeline import run_agent_case

    fake = FakeClient([
        {"action": "t_parse", "args": {"path": "data/raw/002232/03_改错后_2023年年度报告_更正后.pdf"},
         "why": "先把材料解析成原文片段"},
        {"action": "stop", "reason": "证据够用，请求收尾"},
    ])
    out = run_agent_case(CASE, client=fake, max_steps=4, db_paths=_db_paths(tmp_path))

    # 出口必须是三个正当出口之一，不存在「模型说 stop 就算完」的旁路
    assert out.exit in ("stop", "bounds", "budget_exhausted"), out.exit

    run_dir = Path(out.run_dir)
    decisions = [json.loads(l) for l in
                 (run_dir / "decisions.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    assert decisions, "决策日志必须有内容（可重放性的前提）"
    first = decisions[0]
    assert first["action"] == "t_parse"
    assert first["raw_response"], "决策日志要存模型原文响应，不许只存摘要"
    assert first["state_digest"] and first["as_of"] == "2024-04-22"

    # 判定仍由确定性代码做，模型只定调查路径
    assert any("确定性代码" in n for n in out.notes)
    assert out.findings, "自主模式也要能产出结论"


def test_parsed_spans_are_registered_so_extract_is_not_blocked(tmp_path):
    """回归（真模型跑出来的 bug）：``t_parse`` 产出的 spans 必须登记成证据。

    否则 ``state.evidence_index`` 一直为空 → ``t_extract`` 的前置条件永远不满足 →
    模型会把步数全烧在反复解析上（实测 10 步里 6 步是重复解析，一条证据都没登记）。
    """
    from fintrace_agent.pipeline import run_agent_case

    fake = FakeClient([
        {"action": "t_parse",
         "args": {"path": "data/raw/002232/01_改错前_2023年年度报告.pdf",
                  "doc_id": "D-01", "pages": "1-6"},
         "why": "解析材料"},
        {"action": "t_extract", "args": {"doc_id": "D-01"}, "why": "抽取候选断言"},
        {"action": "stop", "reason": "收尾"},
    ])
    out = run_agent_case(CASE, client=fake, max_steps=4, db_paths=_db_paths(tmp_path))
    steps = out.state.steps
    assert steps and steps[0]["action"] == "t_parse" and steps[0]["ok"] is True
    assert steps[0]["new_evidence"], "解析出的 span 必须作为新证据登记"
    assert out.state.evidence_index, "证据索引为空：后面的 t_compute / t_extract 都会被拦"
    second = steps[1] if len(steps) > 1 else {}
    assert second.get("action") == "t_extract"
    assert not str(second.get("error") or "").startswith("precondition"), \
        f"解析成功后 t_extract 不该再被前置条件拦：{second.get('error')}"


def test_extract_candidates_are_bound_to_registered_spans(tmp_path):
    """回归：候选断言必须绑到**已登记的原文片段**（W4 要求数值与页码联合绑定）。

    早先这里传空 spans、`source_spans` 还是自己拼的假 id，于是每一条都被判
    「未绑定任何原文 span」（实测 48/48 全拒）：解析了一整份年报、一条事实没登记。
    """
    from fintrace_agent.pipeline import run_agent_case

    fake = FakeClient([
        {"action": "t_parse",
         "args": {"path": "data/raw/002232/01_改错前_2023年年度报告.pdf",
                  "doc_id": "D-01", "pages": "7-9"},
         "why": "只解析主要会计数据那几页"},
        {"action": "t_extract", "args": {"doc_id": "D-01", "allow_llm": False},
         "why": "抽候选断言"},
        {"action": "stop", "reason": "收尾"},
    ])
    out = run_agent_case(CASE, client=fake, max_steps=3, db_paths=_db_paths(tmp_path))

    steps = {s["action"]: s for s in out.state.steps}
    assert steps["t_parse"]["new_evidence"], "解析出的片段要登记"
    rejected_text = " ".join(str(r.get("why") or "") for r in
                             (steps["t_extract"].get("rejected") or []))
    assert "未绑定任何原文 span" not in rejected_text, rejected_text

    run_dir = Path(out.run_dir)
    rej_path = run_dir / "rejections.jsonl"
    if rej_path.is_file():
        rows = [json.loads(l) for l in rej_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert not any("未绑定任何原文 span" in str(r.get("why") or "") for r in rows)
    assert steps["t_extract"]["new_evidence"] or "已登记" in str(steps["t_extract"].get("message") or ""), \
        f"抽取没有登记任何已核验断言：{steps['t_extract'].get('message')}"


def test_rule_decompose_handles_label_unit_period_value_rows():
    """回归：年报「主要会计数据」表的版式是「科目（单位） 期间 数值」——单位在数值**前面**。

    只认「数值紧跟单位」的话整页表格一条都抽不出来（真模型第 2~7 步全部空手而归，
    回执理由一直是「指标无法归一」）。顺带钉住另一个坑：单位后面先出现的是**年份**，
    直接取第一个数字会把 2023 当成金额。
    """
    from fintrace_agent.knowledge.loader import KnowledgeBase
    from fintrace_agent import paths as P
    from fintrace_agent.tools import registry

    kb = KnowledgeBase(P.KNOWLEDGE_DB)
    text = ("营业收入（元） 2023年 1,243,188,170.21 ；"
            "基本每股收益（元/股） 2023年 0.0718 ；"
            "总资产（元） 2023年末 2,177,194,049.11")

    rec = registry.execute("t_extract", {"text": text, "doc_id": "D-01", "allow_llm": False},
                           {"kb": kb})
    assert rec.ok is True, rec.result
    got = {c["metric"]: (c["value"], c["unit"]) for c in rec.result["claims"]}
    assert got.get("营业收入") == ("1,243,188,170.21", "元"), got
    assert got.get("基本每股收益") == ("0.0718", "元/股"), got
    assert got.get("总资产") == ("2,177,194,049.11", "元"), got


def test_rule_decompose_joins_numbers_split_by_line_break():
    """回归：PDF 文本会把长数字在行末切断（``2,177,194,04\\n9.11``）。

    不拼回来的话 raw_text 不是纯数字串，W6 会判「无法判定披露精度」——
    实测一轮里 18 条就这么被拒了。但也不能把任意空白都吃掉
    （``2023年 1,243`` 会拼成 ``20231,243``）。
    """
    from fintrace_agent.knowledge.loader import KnowledgeBase
    from fintrace_agent import paths as P
    from fintrace_agent.tools import registry

    kb = KnowledgeBase(P.KNOWLEDGE_DB)
    text = "总资产（元） \n2,177,194,04\n9.11 ；\n营业收入（元） 2023年 1,243,188,170.21"
    rec = registry.execute("t_extract", {"text": text, "doc_id": "D-01", "allow_llm": False},
                           {"kb": kb})
    got = {c["metric"]: c["value"] for c in rec.result.get("claims") or []}
    assert got.get("总资产") == "2,177,194,049.11", got
    assert got.get("营业收入") == "1,243,188,170.21", got


def test_t_parse_pages_filter_limits_scope(tmp_path):
    """``pages`` 能限定解析范围：整本解析与单页解析的切片数必须差很多。"""
    from fintrace_agent.tools import registry

    path = "data/raw/002232/01_改错前_2023年年度报告.pdf"
    one = registry.execute("t_parse", {"path": path, "doc_id": "D-01", "pages": [7]}, {})
    assert one.ok is True, one.result
    whole = registry.execute("t_parse", {"path": path, "doc_id": "D-01"}, {})
    assert whole.ok is True, whole.result
    assert one.result["count"] < whole.result["count"], "按页解析应当明显更少"
    assert all(str(s.get("page")) == "7" for s in one.result["spans"])


def test_t_extract_accepts_doc_id_and_reads_registered_spans(tmp_path):
    """回归：只给 doc_id 时，``t_extract`` 要从证据日志拼回正文。

    真模型的做法就是「解析完材料 → 从这份材料抽断言」，手里只有 doc_id；
    原来的契约只认 text/pages，于是每次 t_extract 都 ``bad_args``，白烧几步。
    """
    from fintrace_agent.contracts import Span
    from fintrace_agent.store.evidence import EvidenceLog
    from fintrace_agent.tools import registry

    ev = EvidenceLog("FT-tex")
    ev.add_spans([
        Span(span_id="S1", doc_id="D-01", page=7,
             quoted_text="公司 2023 年实现营业收入 12.43 亿元，同比下降 19.33%。"),
        Span(span_id="S2", doc_id="D-01", page=7,
             quoted_text="归属于上市公司股东的净利润 2,931.56 万元。"),
    ])
    rec = registry.execute("t_extract", {"doc_id": "D-01", "allow_llm": False},
                           {"evidence": ev, "kb": None})
    assert rec.ok is True, rec.result
    assert rec.result["count"] >= 2
    assert rec.result["extracted_by"]["how"] == "rule", "离线时必须走规则拆解，不许上网"


def test_t_extract_without_any_source_says_how_to_fix(tmp_path):
    """没有正文也没给 doc_id 时，报错要写清三条出路（模型据此自纠）。"""
    from fintrace_agent.tools import registry

    rec = registry.execute("t_extract", {}, {"evidence": None, "kb": None})
    assert rec.ok is False and rec.error == "bad_args"
    assert "doc_id" in rec.result["message"] and "pages" in rec.result["message"]


def test_context_carries_documents_tools_and_guidance(tmp_path):
    """上下文必须带「本地材料清单 + 工具手册 + 硬提示」——这是真模型跑通的前提。"""
    from fintrace_agent.agent.planner import build_context
    from fintrace_agent.cases.adapter import documents_of, find_case
    from fintrace_agent.contracts import TaskState

    case = find_case(CASE)
    docs = documents_of(case)
    st = TaskState(run_id="FT-ctx", mode="agent")
    st.as_of = "2024-04-22"
    st.bounds.cutoff_date = "2024-04-22"

    ctx = build_context(st, evidence=None, kb=None, fact_store=None,
                        as_of_mode="historical_replay", documents=docs)

    assert len(ctx["documents"]) == len(docs) >= 2
    late = [d for d in ctx["documents"] if not d["usable"]]
    assert late, "晚于截止日的材料必须标成不可用"
    assert any("晚于截止日" in d["note"] for d in late)
    assert all(d["path"] for d in ctx["documents"]), "材料要带可直接传给 t_parse 的路径"

    by_action = {t["action"]: t for t in ctx["tools"]}
    assert by_action["t_parse"]["required_args"] == ["path"]
    assert by_action["t_search_web"]["available"] is False, "未配置的预留工具必须标明不可用"
    assert by_action["t_extract"]["args"], "工具手册要带参数键，否则模型只能猜"
    assert ctx["guidance"], "硬提示不能为空"


def test_material_chain_parse_extract_gate_on_real_pdf(tmp_path):
    """材料侧整条链路的验收：真实 PDF → 解析登记 → 抽取 → 过 W 门 → 成为事实。

    这条不调模型，测的是「判定与登记」这条确定性链路（模型只决定调查路径）。
    年报「主要会计数据」表的版式在真实 PDF 里是：标签跨行、数值跨行、
    单位写在数值前面 —— 三样都会让朴素抽取整表抽空。
    """
    from fintrace_agent import paths as P
    from fintrace_agent.cases.adapter import documents_of, find_case
    from fintrace_agent.cases.fixed_runner import page_texts_reader
    from fintrace_agent.contracts import Span
    from fintrace_agent.knowledge.loader import KnowledgeBase
    from fintrace_agent.store.evidence import EvidenceLog
    from fintrace_agent.tools import registry
    from fintrace_agent.tools.validator import Validator

    case = find_case(CASE)
    docs = documents_of(case)
    read = page_texts_reader(case)
    kb = KnowledgeBase(P.KNOWLEDGE_DB)

    parsed = registry.execute("t_parse",
                              {"path": docs[0].content_path, "doc_id": docs[0].doc_id,
                               "pages": "7-8"}, {})
    assert parsed.ok is True, parsed.result
    spans = [Span.from_dict(d) for d in parsed.result["spans"]]
    assert spans, "第 7-8 页应当解析出原文片段"

    ev = EvidenceLog("FT-chain")
    ev.add_spans(spans)

    text = "\n".join(s.quoted_text for s in spans)
    ext = registry.execute("t_extract", {"text": text, "doc_id": docs[0].doc_id,
                                         "allow_llm": False}, {"kb": kb, "evidence": ev})
    assert ext.ok is True, ext.result
    claims = ext.result["claims"]
    assert claims, "真实年报的主要会计数据表必须抽出候选"

    # 每条候选绑到真实切片上，再过 W 门
    by_id = {s.span_id: s for s in spans}
    cands = []
    for i, c in enumerate(claims):
        page = c.get("page")
        hit = None
        for s in spans:
            if page is None or s.page == page:
                flat = "".join((s.quoted_text or "").split())
                q = "".join((c.get("quoted_text") or "").split())
                if q and (q in flat or flat in q):
                    hit = s
                    break
        if hit is None:
            continue
        cands.append(Assertion(
            assertion_id=f"FT-chain-{i}", kind="financial_value", subject=case.company,
            period=case.period, metric=c["metric"], metric_id="",
            value=c["value"], unit=c["unit"], raw_text=c["value"],
            text=c.get("quoted_text") or "",
            origin=Origin.MATERIAL.value, source_spans=[hit.span_id]))

    assert cands, "候选必须能绑到已登记的原文切片"
    adm = Validator(kb=kb, cutoff_date=case.prediction_cutoff_date,
                    page_texts=read).admit(cands, spans=spans, doc=docs[0])
    assert adm.accepted, f"真实年报的候选应当有过门的：{adm.rejected[:2]}"
    assert any("未绑定任何原文 span" not in str(r.get("why")) for r in adm.rejected) or adm.accepted



def test_claims_resolve_end_to_end_without_model(tmp_path):
    # 判定层验收（不调模型）：假模型驱动真实链路 —— 解析第 7~8 页 -> 抽取 -> 判定。
    # 用草稿里真实的跨单位主张来验：
    #   营业收入 12.43 亿元  与材料 1,243,188,170.21 元 一致   -> SUPPORTED
    #   基本每股收益 0.72 元 与材料 0.0718 元/股 差 10 倍       -> CONTRADICTED
    # 钉住的坑：期间口径（库里 2023年 / 草稿 2023A）、按字符串查库、单位不换算、精度不对齐。
    from fintrace_agent.facts.store import FactStore
    from fintrace_agent.pipeline import run_agent_case

    fake = FakeClient([
        {"action": "t_parse",
         "args": {"path": "data/raw/002232/01_改错前_2023年年度报告.pdf",
                  "doc_id": "D-01", "pages": "7-8"},
         "why": "定向解析主要会计数据页"},
        {"action": "t_extract", "args": {"doc_id": "D-01", "allow_llm": False},
         "why": "抽出可核验断言"},
        {"action": "stop", "reason": "收尾"},
    ])
    out = run_agent_case(CASE, client=fake, max_steps=3, db_paths=_db_paths(tmp_path))

    # 材料侧必须真的产出「两轴都过」的事实（只过 W 不算）
    store = FactStore(tmp_path / "facts.sqlite")
    facts = store.query(subject="启明信息技术股份有限公司", only_facts=True)
    assert facts, "材料侧应至少产出一条两轴都过的事实"
    got = {str(getattr(a, "metric_id", "") or a.metric): a for a in facts}
    assert "revenue" in got, sorted(got)

    verdicts = {f.claim_id: f.verdict for f in out.findings}
    claims = {c.metric_key: c for c in out.state.claims}
    rev = claims.get("revenue")
    assert rev is not None and verdicts.get(rev.claim_id) == "SUPPORTED", \
        f"营业收入 12.43 亿元 应对得上材料，实判 {verdicts.get(rev.claim_id) if rev else '缺主张'}"
    # 草稿里每股收益有两条：0.72（错，差 10 倍）与 0.0718（对）。两条都要判对。
    eps_bad = next((c for c in out.state.claims
                    if c.metric_key == "basic_eps" and str(c.claim_value).startswith("0.72")), None)
    eps_ok = next((c for c in out.state.claims
                   if c.metric_key == "basic_eps" and str(c.claim_value).startswith("0.0718")), None)
    assert eps_bad is not None and verdicts.get(eps_bad.claim_id) == "CONTRADICTED", \
        f"基本每股收益 0.72 差 10 倍应判矛盾，实判 {verdicts.get(eps_bad.claim_id) if eps_bad else '缺主张'}"
    assert eps_ok is not None and verdicts.get(eps_ok.claim_id) == "SUPPORTED", \
        f"基本每股收益 0.0718 与材料一致应判支持，实判 {verdicts.get(eps_ok.claim_id) if eps_ok else '缺主张'}"
    assert rev.status != "open", "判定结果要回写到主张状态"


def test_metrics_read_from_real_runs_and_split_by_mode(tmp_path):
    """六项指标要从真实 run 产物读得出，且固定链路基线不混进分母。"""
    from fintrace_agent.cases.eval_agent import collect_metrics, render_report
    from fintrace_agent.pipeline import run_agent_case, run_fixed_case

    fixed = run_fixed_case(CASE)
    agent = run_agent_case(CASE, client=FakeClient([
        {"action": "t_parse", "args": {"path": "data/raw/002232/01_改错前_2023年年度报告.pdf"},
         "why": "解析材料"},
        {"action": "stop", "reason": "收尾"},
    ]), max_steps=3, db_paths=_db_paths(tmp_path))

    m = collect_metrics(runs=[fixed.run_dir, agent.run_dir])
    assert agent.run_id in m["runs"]
    assert fixed.run_id not in m["runs"], "固定链路基线不得混进新指标分母"
    assert fixed.run_id in m["excluded_runs"]
    assert m["itr"]["den"] > 0 and m["sr"]["den"] > 0

    text = render_report(m)
    assert "不得外推" in text
    assert "已排除固定链路基线运行" in text
