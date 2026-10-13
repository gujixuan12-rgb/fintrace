"""tests/gate 公共夹具。

只留一个 autouse 夹具（把运行时产物重定向到临时目录）。构造器在 `factories.py`。

为什么分开：多目录套件里 pytest 会把每个测试目录都插进 sys.path，裸名
`from conftest import ...` 会命中最先被导入的那个 conftest（取决于收集顺序），
表现为「全量跑能过、单跑某个目录就 ImportError」。名字唯一的独立模块不受顺序影响。
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def fintrace_home(tmp_path, monkeypatch):
    """把运行时产物（runs/ blobs/）重定向到临时目录。"""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("FINTRACE_HOME", str(home))
    return home
