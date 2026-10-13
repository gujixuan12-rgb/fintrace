"""agent/state —— Task State 原子读写（BUILD_SPEC §8）。"""
from __future__ import annotations

import pytest

from fintrace_agent.agent.state import StateStore
from fintrace_agent.contracts import TaskState, digest


def test_save_load_roundtrip():
    st = StateStore("FT-state-1")
    state = TaskState(run_id="FT-state-1", goal={"case_id": "C1", "company": "启明信息"})
    st.save(state)
    loaded = st.load()
    assert loaded.run_id == "FT-state-1"
    assert loaded.goal == {"case_id": "C1", "company": "启明信息"}


def test_load_missing_returns_empty_state():
    st = StateStore("FT-state-empty")
    state = st.load()
    assert state.run_id == "FT-state-empty"
    assert state.goal == {}


def test_digest_is_deterministic_and_uses_contracts_digest():
    st = StateStore("FT-state-2")
    a = TaskState(run_id="FT-state-2", goal={"x": 1})
    b = TaskState(run_id="FT-state-2", goal={"x": 1})
    assert st.digest(a) == st.digest(b)
    assert st.digest(a) == digest(a.to_dict())


class _Unserializable:
    """to_dict 返回含 set 的对象 → json.dumps 会抛 TypeError。"""

    def to_dict(self):
        return {"bad": {1, 2, 3}}


def test_atomic_write_keeps_old_file_on_failure():
    """写到一半失败不留坏文件：旧文件仍是上一次的完整内容。"""
    st = StateStore("FT-state-3")
    good = TaskState(run_id="FT-state-3", goal={"ok": True})
    st.save(good)

    with pytest.raises(TypeError):
        st.save(_Unserializable())

    # 旧文件仍可读回且是完整的
    assert st.load().goal == {"ok": True}
    # 没有残留临时文件
    assert list(st.dir.glob(".tmp-*")) == []


def test_atomic_write_cleans_temp_when_replace_interrupted(monkeypatch):
    """模拟「落盘途中被杀」：os.replace 抛 KeyboardInterrupt，旧文件不受影响、临时文件被清。"""
    import fintrace_agent.store.runs as runs_mod

    st = StateStore("FT-state-4")
    st.save(TaskState(run_id="FT-state-4", goal={"v": 1}))

    def boom(src, dst):
        raise KeyboardInterrupt("killed mid-write")

    monkeypatch.setattr(runs_mod.os, "replace", boom)
    with pytest.raises(KeyboardInterrupt):
        st.save(TaskState(run_id="FT-state-4", goal={"v": 2}))

    assert st.load().goal == {"v": 1}                     # 旧内容仍在
    assert list(st.dir.glob(".tmp-*")) == []              # 临时文件被清
