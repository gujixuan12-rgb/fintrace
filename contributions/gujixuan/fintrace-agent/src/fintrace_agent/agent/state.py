"""Task State 的原子读写（对应 BUILD_SPEC §8 · v2.2 §3.1）。

只做一件事：把 ``TaskState`` 原子地落盘到 ``runs/<run_id>/state.json`` 并读回。
原子性由 ``store.runs.atomic_write_json`` 保证（同目录临时文件 + ``os.replace``）：
写到一半被杀不会留半截文件，旧文件仍是上一次的完整内容。

本模块不解释状态语义（那是 bounds / planner / loop 的事）。
"""
from __future__ import annotations

from ..contracts import TaskState, digest
from ..store.runs import atomic_write_json, read_json, run_dir


class StateStore:
    """单个 run 的 ``state.json`` 读写门面。"""

    def __init__(self, run_id: str, create: bool = True):
        self.run_id = (run_id or "").strip()
        if not self.run_id:
            raise ValueError("run_id 不能为空")
        self.dir = run_dir(self.run_id, create=create)
        self.path = self.dir / "state.json"

    def save(self, state: TaskState) -> None:
        """原子写 ``state.json``；写完必可读，绝不留半截文件。"""
        atomic_write_json(self.path, state.to_dict())

    def load(self) -> TaskState:
        """读回 ``state.json``；文件不存在或为空时返回一个空状态（run_id 已填）。"""
        data = read_json(self.path)
        if not data:
            return TaskState(run_id=self.run_id)
        return TaskState.from_dict(data)

    def digest(self, state: TaskState) -> str:
        """state 的稳定摘要（走 ``contracts.digest``）；重放一致性靠它。"""
        return digest(state.to_dict())


__all__ = ["StateStore"]
