"""agent/model —— 决策解析、key 掩码、未配置明确失败（BUILD_SPEC §8）。"""
from __future__ import annotations

import pytest

from fintrace_agent.agent.model import (
    DecisionError,
    ModelClient,
    NotConfigured,
    mask_key,
    parse_decision,
)


# ---------------------------------------------------------------------------
# parse_decision
# ---------------------------------------------------------------------------


def test_parse_decision_valid():
    d = parse_decision('{"action":"t_knowledge","args":{"query":"x"},"why":"w"}')
    assert d == {"action": "t_knowledge", "args": {"query": "x"}, "why": "w"}


def test_parse_decision_tolerates_code_fence():
    d = parse_decision('```json\n{"action":"stop","args":{}}\n```')
    assert d["action"] == "stop"
    assert d["args"] == {}


@pytest.mark.parametrize("bad", ["", "not json", "[1,2]", '{"args":{}}', '{"action":"x","args":5}'])
def test_parse_decision_rejects(bad):
    with pytest.raises(DecisionError):
        parse_decision(bad)


# ---------------------------------------------------------------------------
# key 掩码与不泄漏
# ---------------------------------------------------------------------------


def test_mask_key():
    assert mask_key("") == ""
    assert mask_key("short") == "***"
    assert mask_key("abcdefgh12345678") == "abcd...5678"


def test_unconfigured_client_fails_explicitly_without_leaking_key():
    # 有 key 但没有 model → 仍未配置；异常里不得出现 key 明文
    client = ModelClient(api_key="SECRETKEY12345", model="", base_url="")
    assert client.configured is False
    with pytest.raises(NotConfigured) as ei:
        client.decide({"any": "context"})
    msg = str(ei.value)
    assert "SECRETKEY12345" not in msg
    assert "SECR...2345" in msg


def test_configured_flag_requires_key_and_model():
    assert ModelClient().configured is False
    assert ModelClient(api_key="k123456789", model="m").configured is True


def test_explain_unconfigured_raises():
    with pytest.raises(NotConfigured):
        ModelClient().explain({"a": 1})
