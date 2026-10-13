"""agent/planner —— 上下文六块 + as_of 线索过滤 + 决策（BUILD_SPEC §8）。"""
from __future__ import annotations

from fintrace_agent.agent.planner import build_context, decide, filter_leads_by_as_of
from fintrace_agent.contracts import AsOfMode, Bounds, Claim, TaskState, digest


def mk_state(**kw) -> TaskState:
    return TaskState(run_id="FT-plan-1", bounds=Bounds(cutoff_date="2023-06-30"),
                     as_of="2023-06-30", **kw)


class FakeClient:
    model = "fake"

    def __init__(self, response):
        self.response = response

    @property
    def configured(self):
        return True

    def decide(self, context):
        return self.response, {"total_tokens": 7}


def test_build_context_has_six_blocks_and_contract():
    claim = Claim(claim_id="C1", claim_text="...", metric_key="净利润",
                  open_questions=["Q1 数字对不对"])
    ctx = build_context(mk_state(claims=[claim]), evidence=None, kb=None, fact_store=None,
                        as_of_mode=AsOfMode.HISTORICAL_REPLAY.value)
    assert ctx["header"]["case_id"] == ""
    assert ctx["header"]["as_of_mode"] == AsOfMode.HISTORICAL_REPLAY.value
    assert ctx["open_questions"] == ["[C1] Q1 数字对不对"]
    assert "output_contract" in ctx and ctx["output_contract"]
    assert ctx["context_leads_span"] == ("", "")
    for block in ("header", "open_questions", "spans", "rules", "facts",
                  "last_receipt", "leads"):
        assert block in ctx


def test_leads_filtered_by_as_of_historical_replay():
    """晚于 cutoff 的线索不进上下文；无首曝时间的线索一律不可用。"""
    leads = [
        {"lead_id": "L1", "url": "u1", "title": "早", "first_published_at": "2023-01-01"},
        {"lead_id": "L2", "url": "u2", "title": "晚", "first_published_at": "2023-12-31"},
        {"lead_id": "L3", "url": "u3", "title": "无时间", "first_published_at": ""},
    ]
    kept, span = filter_leads_by_as_of(
        leads, as_of_mode=AsOfMode.HISTORICAL_REPLAY.value, cutoff="2023-06-30")
    assert [x["lead_id"] for x in kept] == ["L1"]
    assert span == ("2023-01-01", "2023-01-01")

    ctx = build_context(mk_state(), evidence=None, kb=None, fact_store=None,
                        as_of_mode=AsOfMode.HISTORICAL_REPLAY.value, leads=leads)
    assert [x["lead_id"] for x in ctx["leads"]] == ["L1"]
    assert ctx["context_leads_span"] == ("2023-01-01", "2023-01-01")


def test_leads_kept_all_in_post_hoc_review():
    leads = [
        {"lead_id": "L1", "first_published_at": "2023-01-01"},
        {"lead_id": "L2", "first_published_at": "2023-12-31"},
    ]
    kept, span = filter_leads_by_as_of(
        leads, as_of_mode=AsOfMode.POST_HOC_REVIEW.value, cutoff="2023-06-30")
    assert {x["lead_id"] for x in kept} == {"L1", "L2"}
    assert span == ("2023-01-01", "2023-12-31")


def test_decide_valid_carries_raw_response_digest_as_of_and_span():
    state = mk_state()
    ctx = {"context_leads_span": ("2023-01-01", "2023-01-01"), "output_contract": "c"}
    d = decide(state, FakeClient('{"action":"t_recall","args":{"keyword":"x"},"why":"w"}'),
               context=ctx)
    assert d.valid is True
    assert d.action == "t_recall"
    assert d.args == {"keyword": "x"}
    assert d.raw_response == '{"action":"t_recall","args":{"keyword":"x"},"why":"w"}'
    assert d.state_digest == digest(state.to_dict())
    assert d.as_of == "2023-06-30"
    assert d.context_leads_span == ("2023-01-01", "2023-01-01")
    assert getattr(d, "usage") == {"total_tokens": 7}


def test_decide_invalid_json_and_unknown_action():
    state = mk_state()
    ctx = {"context_leads_span": ("", "")}
    d1 = decide(state, FakeClient("nonsense"), context=ctx)
    assert d1.valid is False and d1.invalid_reason

    d2 = decide(state, FakeClient('{"action":"hack_the_planet","args":{}}'), context=ctx)
    assert d2.valid is False
    assert "白名单" in d2.invalid_reason
