"""tests/facts 公共夹具：临时目录里的 FactStore 与样例构造器。

硬约束：数据库写临时目录，绝不污染项目目录。
"""
from __future__ import annotations

from pathlib import Path

import pytest

# pytest 的 pythonpath 已在 pyproject.toml 配好（src），这里直接 import。
from fintrace_agent.facts import FactStore


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    """每个测试一个独立的临时 SQLite 文件。"""
    return tmp_path / "facts.sqlite"


@pytest.fixture()
def store(db_path: Path):
    s = FactStore(db_path)
    yield s
    s.close()



