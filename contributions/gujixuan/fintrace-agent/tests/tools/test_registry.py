"""工具注册表测试：注册完整性、拒绝语义、异常兜底、大结果落 blobs、联网红线。

绝不触网：预留联网工具在未配置时只允许返回 ``not_configured``，本文件把 ``urlopen`` /
``socket`` 换成炸弹来证明「一次网络调用都没发生」。
"""
from __future__ import annotations

import socket
import urllib.request

import pytest

from fintrace_agent.store import blobs
from fintrace_agent.tools import registry
from fintrace_agent.tools.registry import (ToolContext, ToolError, ToolOutcome,
                                           ToolSpec, execute)

#: BUILD_SPEC 第 7 节列出的 12 件工具（不含 stop）
EXPECTED = {
    "t_search_official", "t_search_web", "t_fetch_web", "t_parse", "t_extract",
    "t_knowledge", "t_compute", "t_review", "t_lookup", "t_recall",
    "t_spec_extract", "t_author_tool",
}


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    """把 blobs / runs 重定向到临时目录。"""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("FINTRACE_HOME", str(home))
    return home


@pytest.fixture
def no_network(monkeypatch):
    """任何网络调用都直接抛断言——用来证明工具没触网。"""
    def _boom(*args, **kwargs):  # pragma: no cover - 触网才会走到
        raise AssertionError("发生了网络调用！")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    return _boom


def test_all_tools_registered():
    names = {s.name for s in registry.all_specs()}
    assert names == EXPECTED
    for name in EXPECTED:
        assert registry.get(name) is not None
        assert registry.get(name).name == name


def test_spec_defaults():
    spec = ToolSpec(name="x", params={}, receipt={}, preconditions=[])
    assert spec.timeout_s == 20
    assert spec.enabled is True
    assert spec.reserved is False


def test_disabled_tools_flags():
    assert registry.get("t_author_tool").enabled is False
    assert registry.get("t_review").enabled is False
    assert registry.get("t_search_web").reserved is True
    assert registry.get("t_fetch_web").reserved is True


def test_unknown_tool_rejected():
    r = execute("t_does_not_exist", {}, ToolContext())
    assert r.ok is False
    assert r.error == "unknown_tool"
    assert r.tool == "t_does_not_exist"


def test_author_tool_disabled_not_executed():
    r = execute("t_author_tool", {}, ToolContext())
    assert r.ok is False
    assert r.error == "disabled"


def test_review_disabled():
    r = execute("t_review", {}, ToolContext())
    assert r.ok is False
    assert r.error == "disabled"


@pytest.mark.parametrize("name", ["t_search_web", "t_fetch_web"])
def test_reserved_web_not_configured(name):
    r = execute(name, {"query": "x", "url": "http://example.com"}, ToolContext())
    assert r.ok is False
    assert r.error == "not_configured"


def test_reserved_web_do_not_touch_network(no_network):
    # 未配置 → 根本不调用处理器，也就不可能触网
    for name, args in (("t_search_web", {"query": "x"}),
                       ("t_fetch_web", {"url": "http://example.com"})):
        r = execute(name, args, ToolContext())
        assert r.error == "not_configured"


def test_reserved_web_run_injected_backend_without_net(no_network):
    calls: list = []
    ctx = ToolContext(config={
        "web": {"enabled": True, "search": lambda q: calls.append(q) or [{"url": "u"}]}
    })
    r = execute("t_search_web", {"query": "x"}, ctx)
    assert r.ok is True
    assert r.result["leads"] == [{"url": "u"}]
    assert calls == [{"query": "x", "limit": 5}]


def test_execute_catches_tool_exception():
    def _boom(args, ctx):
        raise RuntimeError("处理器炸了")

    registry.register(ToolSpec(name="t_boom_test", params={}, receipt={}, preconditions=[]), _boom)
    try:
        r = execute("t_boom_test", {}, ToolContext())
        assert r.ok is False
        assert r.error == "tool_exception"
        assert "RuntimeError" in r.result["message"]
    finally:
        registry.unregister("t_boom_test")


def test_execute_maps_toolerror_code():
    def _terr(args, ctx):
        raise ToolError("bad_args", "参数不对")

    registry.register(ToolSpec(name="t_terr_test", params={}, receipt={}, preconditions=[]), _terr)
    try:
        r = execute("t_terr_test", {}, ToolContext())
        assert r.ok is False
        assert r.error == "bad_args"
        assert r.result["message"] == "参数不对"
    finally:
        registry.unregister("t_terr_test")


def test_execute_rejects_non_dict_args():
    r = execute("t_knowledge", ["not", "a", "dict"], ToolContext())
    assert r.ok is False
    assert r.error == "bad_args"


def test_execute_records_ms_and_success():
    r = execute("t_author_tool", {}, ToolContext())
    assert isinstance(r.ms, int) and r.ms >= 0
    # 成功路径的收据（注入搜索后端）也要有 ms
    ctx = ToolContext(config={"web": {"enabled": True, "search": lambda q: []}})
    ok_r = execute("t_search_web", {"query": "x"}, ctx)
    assert ok_r.ok is True and ok_r.ms >= 0


def test_big_result_offloaded_to_blobs():
    payload = "公" * 9000  # 超过 BIG_TEXT_LIMIT

    def _big(args, ctx):
        return ToolOutcome(result={"huge": payload, "small": "ok"})

    registry.register(ToolSpec(name="t_big_test", params={}, receipt={}, preconditions=[]), _big)
    try:
        r = execute("t_big_test", {}, ToolContext())
        assert r.ok is True
        assert len(r.blob_refs) == 1
        ref = r.result["huge"]
        assert ref["blob_ref"] == r.blob_refs[0]
        assert ref["type"] == "text"
        assert r.result["small"] == "ok"
        # 内容寻址库能取回原文
        assert blobs.get_text(r.blob_refs[0]) == payload
    finally:
        registry.unregister("t_big_test")
