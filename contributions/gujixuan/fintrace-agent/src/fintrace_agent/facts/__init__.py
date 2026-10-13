"""facts 子包 —— 公司事实库（实例层，模块 C）。

对外只暴露 :class:`FactStore`。数据类一律来自 ``fintrace_agent.contracts``，
本包不自造字段、不改契约。
"""
from __future__ import annotations

from .store import FactStore

__all__ = ["FactStore"]
