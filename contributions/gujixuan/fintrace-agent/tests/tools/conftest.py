"""tests/tools 公共夹具（模块 E）。

知识库自洽：现场用引擎生成 YAML，再编译进 tmp_path 里的临时 SQLite，不依赖仓库已提交的数据文件。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 兜底：即使没走 pyproject 的 pythonpath 也能 import
_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from fintrace_agent.knowledge import build, extract_from_engine  # noqa: E402
from fintrace_agent.knowledge.loader import KnowledgeBase  # noqa: E402


@pytest.fixture
def yaml_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    extract_from_engine.generate(d)
    return d


@pytest.fixture
def db_path(yaml_dir: Path, tmp_path: Path) -> Path:
    db = tmp_path / "knowledge.sqlite"
    build.build_db(yaml_dir, db)
    return db


@pytest.fixture
def kb(db_path: Path):
    k = KnowledgeBase(db_path)
    yield k
    k.close()
