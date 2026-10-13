"""store 测试的公共夹具。

每个测试都跑在独立的临时存储根下：通过 ``FINTRACE_HOME`` 环境变量把
blobs/ runs/ snapshots/ 全部重定向到 tmp_path，测试之间互不干扰。
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def fintrace_home(tmp_path, monkeypatch):
    """把 store 包重定向到临时目录（套件级自动生效）。"""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("FINTRACE_HOME", str(home))
    return home
