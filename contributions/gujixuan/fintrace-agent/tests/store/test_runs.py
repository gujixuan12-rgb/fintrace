"""store/runs.py 测试：运行号形态、目录、JSON 原子写与读回。"""
from __future__ import annotations

import os
import re
from datetime import datetime

import pytest

from fintrace_agent.store import runs


def test_new_run_id_shape():
    rid = runs.new_run_id()
    assert re.fullmatch(r"FT-\d{8}-[0-9a-f]{8}", rid), rid


def test_new_run_id_uses_today():
    today = datetime.now().strftime("%Y%m%d")
    assert runs.new_run_id().split("-")[1] == today


def test_new_run_id_unique():
    assert len({runs.new_run_id() for _ in range(50)}) == 50


def test_run_dir_not_created_unless_asked(fintrace_home):
    path = runs.run_dir("FT-20261010-deadbeef")
    assert path.name == "FT-20261010-deadbeef"
    assert path.parent.name == "runs"
    assert not path.exists()


def test_run_dir_create_makes_fixed_subdirs(fintrace_home):
    path = runs.run_dir("FT-20261010-deadbeef", create=True)
    assert path.is_dir()
    assert (path / "snapshots").is_dir()
    assert (path / "tools").is_dir()


def test_run_dir_rejects_traversal():
    for bad in ("", "  ", "../x", "a/b", "a\\b"):
        with pytest.raises(ValueError):
            runs.run_dir(bad)


def test_atomic_write_json_readable(tmp_path):
    p = tmp_path / "state.json"
    runs.atomic_write_json(p, {"run_id": "x", "n": 1, "中文": "值"})
    assert runs.read_json(p) == {"run_id": "x", "n": 1, "中文": "值"}


def test_atomic_write_json_uses_tempfile_then_replace(tmp_path, monkeypatch):
    calls = []
    real_replace = os.replace

    def spy(src, dst):
        calls.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    p = tmp_path / "state.json"
    runs.atomic_write_json(p, {"a": 1})
    assert calls
    src, dst = calls[-1]
    assert os.path.basename(dst) == "state.json"
    assert os.path.dirname(src) == os.path.dirname(dst)
    assert os.path.basename(src).startswith(".tmp-")


def test_atomic_write_json_crash_leaves_no_partial_file(tmp_path, monkeypatch):
    p = tmp_path / "state.json"

    def boom(*_a, **_k):
        raise OSError("模拟断电")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        runs.atomic_write_json(p, {"a": 1})

    # 目标文件不存在，且没有残留临时文件
    assert not p.exists()
    assert [n for n in os.listdir(tmp_path) if n.startswith(".tmp-")] == []


def test_atomic_write_json_crash_keeps_previous_content(tmp_path, monkeypatch):
    p = tmp_path / "state.json"
    runs.atomic_write_json(p, {"v": "old"})

    def boom(*_a, **_k):
        raise OSError("模拟断电")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        runs.atomic_write_json(p, {"v": "new"})

    # 旧内容完整保留，绝不出现半截文件
    assert runs.read_json(p) == {"v": "old"}


def test_read_json_missing_returns_empty(tmp_path):
    assert runs.read_json(tmp_path / "nope.json") == {}


def test_read_json_empty_file_returns_empty(tmp_path):
    p = tmp_path / "empty.json"
    p.write_text("", encoding="utf-8")
    assert runs.read_json(p) == {}


def test_append_and_read_jsonl(tmp_path):
    p = tmp_path / "decisions.jsonl"
    runs.append_jsonl(p, {"n": 1, "action": "t_parse"})
    runs.append_jsonl(p, {"n": 2, "action": "t_compute"})
    got = runs.read_jsonl(p)
    assert [r["n"] for r in got] == [1, 2]


def test_read_jsonl_skips_truncated_trailing_line(tmp_path):
    p = tmp_path / "decisions.jsonl"
    runs.append_jsonl(p, {"n": 1})
    # 模拟崩溃留下的半行
    with p.open("a", encoding="utf-8") as f:
        f.write('{"n": 2, "act')
    got = runs.read_jsonl(p)
    assert [r["n"] for r in got] == [1]
