"""存储根目录解析 —— 已统一到 `fintrace_agent.paths`（单一来源，别再自己算一遍）。

保留这四个名字，是为了让 store 各模块与 `tests/store` 的 `FINTRACE_HOME` 隔离继续有效。
与旧版的唯一区别：缺省根**不再是当前工作目录，而是项目根** —— 按 cwd 会让在别的目录
启动的 CLI 把产物写到别处，事后按 run_id 找不回来。
"""
from __future__ import annotations

from fintrace_agent.paths import blobs_dir, home, runs_dir, snapshots_dir

__all__ = ["home", "blobs_dir", "runs_dir", "snapshots_dir"]
