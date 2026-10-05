"""大模型层测试。

默认**离线**：用假 response 顶掉 urllib，覆盖解析、掩码、缺 key、报错分支，
不依赖网络也不读本机真 key。

要真调一次接口：

    FINTRACE_NET_TESTS=1 python -m pytest tests/test_llm.py
"""
from __future__ import annotations

import json
import os
import urllib.error

import pytest

from fintrace import llm


class _FakeResponse:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture()
def isolated_config(tmp_path, monkeypatch):
    """把配置指向临时文件，避免读到本机真实的 .secrets/llm.json。"""
    cfg_file = tmp_path / "llm.json"
    monkeypatch.setattr(llm, "CONFIG_FILE", cfg_file)
    monkeypatch.setattr(llm, "ENV_FILE", tmp_path / ".env")
    for name in llm._ENV_KEYS:
        monkeypatch.delenv(name, raising=False)
    return cfg_file


def _write(cfg_file, **kw) -> None:
    cfg_file.write_text(json.dumps(kw, ensure_ascii=False), encoding="utf-8")


def _fake_ok(content: str):
    return lambda req, timeout=None: _FakeResponse(
        {"choices": [{"message": {"content": content}}]}
    )


# ------------------------------------------------------------------ 配置与掩码


def test_mask_never_leaks_plaintext():
    key = "sk-" + "abcdefghijklmnop" + "2d76"
    masked = llm.mask(key)
    assert masked.startswith("sk-")
    assert masked.endswith("2d76")
    assert key not in masked
    assert "abcdefghijklmnop" not in masked


def test_public_config_reports_presence_not_key(isolated_config):
    key = "sk-" + "z" * 30
    _write(isolated_config, base_url="https://api.deepseek.com", model="deepseek-chat", api_key=key)
    view = llm.public_config()
    assert view["has_key"] is True
    assert view["key_hint"]
    assert key not in json.dumps(view, ensure_ascii=False)


def test_missing_key_raises_llm_error(isolated_config):
    with pytest.raises(llm.LLMError):
        llm.chat([{"role": "user", "content": "hi"}])
    assert llm.available() is False


def test_env_var_used_when_no_file_key(isolated_config, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-" + "e" * 30)
    cfg = llm.load_config()
    assert cfg["api_key"].startswith("sk-")
    assert cfg["_key_from"] == "DEEPSEEK_API_KEY"


def test_available_true_with_key(isolated_config):
    _write(isolated_config, api_key="sk-" + "k" * 30)
    assert llm.available() is True


# ------------------------------------------------------------------ 调用


def test_chat_parses_openai_compatible_shape(isolated_config, monkeypatch):
    _write(isolated_config, api_key="sk-" + "k" * 30)
    seen: dict = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["auth"] = req.headers.get("Authorization")
        seen["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResponse({"choices": [{"message": {"content": " 可用 "}}]})

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    assert llm.chat([{"role": "user", "content": "hi"}]) == "可用"
    assert seen["url"].endswith("/chat/completions")
    assert seen["auth"].startswith("Bearer sk-")
    assert seen["body"]["stream"] is False


def test_http_error_becomes_llm_error(isolated_config, monkeypatch):
    _write(isolated_config, api_key="sk-" + "k" * 30)

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(llm.LLMError) as excinfo:
        llm.chat([{"role": "user", "content": "hi"}])
    assert "401" in str(excinfo.value)


def test_bad_shape_becomes_llm_error(isolated_config, monkeypatch):
    _write(isolated_config, api_key="sk-" + "k" * 30)
    monkeypatch.setattr(llm.urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeResponse({"unexpected": True}))
    with pytest.raises(llm.LLMError):
        llm.chat([{"role": "user", "content": "hi"}])


# ------------------------------------------------------------------ 拆断言


def test_decompose_parses_claims_and_drops_blank(isolated_config, monkeypatch):
    _write(isolated_config, api_key="sk-" + "k" * 30)
    payload = {"claims": [
        {"claim_text": "每 10 股派息 0.10 元", "value": "0.10", "page": 51},
        {"claim_text": "   "},
    ]}
    monkeypatch.setattr(llm.urllib.request, "urlopen", _fake_ok(json.dumps(payload, ensure_ascii=False)))
    claims = llm.decompose_claims("第 51 页 每 10 股派息 0.10 元", source="年报.pdf 第 51 页")
    assert len(claims) == 1
    assert claims[0]["claim_text"] == "每 10 股派息 0.10 元"
    assert claims[0]["page"] == 51


def test_decompose_rejects_non_json(isolated_config, monkeypatch):
    _write(isolated_config, api_key="sk-" + "k" * 30)
    monkeypatch.setattr(llm.urllib.request, "urlopen", _fake_ok("这不是 JSON"))
    with pytest.raises(llm.LLMError):
        llm.decompose_claims("材料正文")


def test_decompose_rejects_empty_text(isolated_config):
    _write(isolated_config, api_key="sk-" + "k" * 30)
    with pytest.raises(llm.LLMError):
        llm.decompose_claims("   ")


# ------------------------------------------------------------------ 联网（默认跳过）


@pytest.mark.skipif(os.environ.get("FINTRACE_NET_TESTS") != "1",
                    reason="联网测试：设 FINTRACE_NET_TESTS=1 打开")
def test_real_api_roundtrip():
    if not llm.available():
        pytest.skip("本机没有配置 key")
    assert llm.chat([{"role": "user", "content": "只回复两个字：可用"}])


@pytest.mark.skipif(os.environ.get("FINTRACE_NET_TESTS") != "1",
                    reason="联网测试：设 FINTRACE_NET_TESTS=1 打开")
def test_real_api_explains_deterministic_result():
    if not llm.available():
        pytest.skip("本机没有配置 key")
    text = llm.explain(
        annual_file="测试材料.pdf",
        meta={"复算过程": {"expected_yuan": "4085484.55"}},
        findings=[{"code": "D1-inconsistent", "severity": "error", "title": "分红金额对不上",
                   "detail": "披露 408548.46 元，复算 4085484.55 元",
                   "evidence": [{"file": "测试材料.pdf", "page": 51, "line": "现金分红金额 408548.46"}]}],
        report_markdown="# 报告\n复算值 4085484.55 元",
    )
    assert text
    assert "4085484.55" in text.replace(",", "")
