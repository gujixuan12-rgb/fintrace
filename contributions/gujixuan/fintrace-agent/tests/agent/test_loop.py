"""agent/loop —— 回路出口、两轮落盘、回落 fixed、无 stop 旁路（BUILD_SPEC §8）。"""
from __future__ import annotations

import json

from fintrace_agent.agent.loop import run_loop
from fintrace_agent.contracts import ToolReceipt
from fintrace_agent.store.runs import read_jsonl, run_dir

CASE = {"case_id": "CASE-1", "company": "启明信息", "period": "2023A",
        "prediction_cutoff_date": "2023-06-30"}


class FakeClient:
    """返回固定 JSON 的假模型；不发任何 HTTP 请求。"""

    model = "fake-model"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    @property
    def configured(self):
        return True

    def decide(self, context):
        self.calls += 1
        r = self.responses[min(self.calls - 1, len(self.responses) - 1)]
        return r, {"total_tokens": 11}


def fake_execute(name, args, ctx):
    new = [f"sp-{name}"] if name == "t_recall" else []
    return ToolReceipt(tool=name, ok=True, new_evidence=new)


# ---------------------------------------------------------------------------
# 两轮回路：落盘 state.json 与 decisions.jsonl
# ---------------------------------------------------------------------------


def test_two_round_loop_persists_state_and_decisions():
    client = FakeClient([
        '{"action":"t_recall","args":{"keyword":"现金分红"},"why":"取回原文"}',
        '{"action":"stop","args":{},"why":"证据足够"}',
    ])
    res = run_loop(case=CASE, client=client, mode="agent",
                   run_id="FT-loop-01", tools_ctx={"execute": fake_execute})

    assert res.exit == "stop"
    assert len(res.decisions) == 2
    # 出口 (b) 必须经过 V 门（即便默认降级放行，gate 结果非空）
    assert res.gate is not None

    d = run_dir("FT-loop-01")
    # state.json 落盘
    state_file = d / "state.json"
    assert state_file.is_file()
    state_obj = json.loads(state_file.read_text(encoding="utf-8"))
    assert state_obj["run_id"] == "FT-loop-01"
    assert state_obj["counting"]["tool_calls"] == 1
    assert state_obj["stop"]["requested_by_model"] is True

    # decisions.jsonl：含模型原文响应 / state_digest / as_of
    rows = read_jsonl(d / "decisions.jsonl")
    assert len(rows) == 2
    assert rows[0]["raw_response"].startswith('{"action":"t_recall"')
    assert rows[0]["state_digest"]
    assert rows[0]["as_of"] == "2023-06-30"
    assert rows[1]["action"] == "stop"
    assert rows[1]["state_digest"]
    # 决策日志完整（可重放）：模型名与 prompt 版本也在
    assert rows[0]["model"] == "fake-model"
    assert rows[0]["prompt_version"] == "p1"


def test_stop_records_context_leads_span_only_from_kept_leads():
    """historical_replay：晚于 cutoff 的线索不进上下文，Decision 记首曝区间。"""
    client = FakeClient(['{"action":"stop","args":{},"why":"done"}'])
    leads = [
        {"lead_id": "L1", "first_published_at": "2023-01-01"},
        {"lead_id": "L2", "first_published_at": "2023-12-31"},   # 晚于 cutoff
    ]
    res = run_loop(case=CASE, client=client, mode="agent", run_id="FT-loop-02",
                   tools_ctx={"execute": fake_execute, "leads": leads})
    rows = read_jsonl(run_dir("FT-loop-02") / "decisions.jsonl")
    assert res.exit == "stop"
    assert rows[0]["context_leads_span"] == ["2023-01-01", "2023-01-01"]


# ---------------------------------------------------------------------------
# 连续 3 次非法决策 → 回落 fixed（断言假 fixed_runner 被调到）
# ---------------------------------------------------------------------------


def test_three_invalid_decisions_fall_back_to_fixed():
    called = {}

    def fake_fixed(case, state):
        called["hit"] = True
        called["run_id"] = state.run_id
        return {"ok": True}

    client = FakeClient(["this is not json"])          # 每轮都非法
    res = run_loop(case=CASE, client=client, mode="agent", run_id="FT-loop-03",
                   tools_ctx={"execute": fake_execute, "fixed_runner": fake_fixed})

    assert res.exit == "fixed"
    assert called.get("hit") is True
    assert res.state.counting["invalid_decisions"] == 3
    assert len(res.decisions) == 3
    assert rows_of("FT-loop-03")  # 非法决策也进日志，供审计


def test_agent_without_model_falls_back_to_fixed():
    called = {}

    def fake_fixed(case, state):
        called["hit"] = True

    res = run_loop(case=CASE, client=None, mode="agent", run_id="FT-loop-04",
                   tools_ctx={"fixed_runner": fake_fixed})
    assert res.exit == "fixed"
    assert called.get("hit") is True


# ---------------------------------------------------------------------------
# 没有「模型说 stop 就直接完」的旁路：V 门不认可就不能以 stop 收尾
# ---------------------------------------------------------------------------


def test_no_stop_bypass_when_v_gate_requires_more():
    def gate_requires_more(findings, state):
        return {"approve": False, "needs_more": True, "residuals": [{"q": "未解"}]}

    client = FakeClient(['{"action":"stop","args":{},"why":"done"}'])  # 每轮都请求 stop
    res = run_loop(case=CASE, client=client, mode="agent", run_id="FT-loop-05",
                   max_steps=3,
                   tools_ctx={"execute": fake_execute, "gate": gate_requires_more})

    # V 门要求补查 + 无更多额度 → 带着残余出报告，绝不是「模型说 stop 就结束」
    assert res.exit != "stop"
    assert res.exit in {"budget_exhausted", "bounds"}


def test_stop_exit_requires_v_gate_consulted():
    """结构断言：走 stop 出口的那次 run，一定调用过 V 门（gate 结果非空）。"""
    client = FakeClient(['{"action":"stop","args":{},"why":"done"}'])
    seen = {}

    def gate_approve(findings, state):
        seen["called"] = True
        return {"approve": True}

    res = run_loop(case=CASE, client=client, mode="agent", run_id="FT-loop-06",
                   tools_ctx={"execute": fake_execute, "gate": gate_approve})
    assert res.exit == "stop"
    assert seen.get("called") is True
    assert res.gate == {"approve": True}


def test_tool_permission_outside_allowlist_blocks_loop():
    """模型请求允许集之外的动作 → 硬边界 BLOCK，出口 bounds。"""
    client = FakeClient(['{"action":"t_author_tool","args":{},"why":"自写工具"}'])
    res = run_loop(case=CASE, client=client, mode="agent", run_id="FT-loop-07",
                   tools_ctx={"execute": fake_execute})
    assert res.exit == "bounds"
    assert res.state.steps[-1]["error"] == "tool_permission"


def rows_of(run_id):
    return read_jsonl(run_dir(run_id) / "decisions.jsonl")
