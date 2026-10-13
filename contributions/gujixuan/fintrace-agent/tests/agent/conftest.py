"""agent 模块测试的公共夹具：隔离落盘目录、清空模型 key（保证不触网）。"""
from __future__ import annotations

import pytest

_ENV_KEYS = (
    "FINTRACE_LLM_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY",
    "FINTRACE_LLM_BASE_URL", "FINTRACE_LLM_MODEL",
)


@pytest.fixture(autouse=True)
def isolate_home(tmp_path, monkeypatch):
    """每个用例把运行时产物重定向到临时目录，并清掉可能存在的 LLM key。"""
    monkeypatch.setenv("FINTRACE_HOME", str(tmp_path / "home"))
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    # 让 .secrets/llm.json 指向不存在的文件，避免真实 key 渗进测试
    from fintrace_agent import paths
    monkeypatch.setattr(paths, "SECRETS", tmp_path / "no-such-secrets.json", raising=False)
    yield tmp_path
