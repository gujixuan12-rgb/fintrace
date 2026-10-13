"""store —— 原文层与落盘（模块 A）。

四个子模块职责：
- ``blobs``     内容寻址的对象存储（blobs/<前两位>/<sha>.bin）；
- ``paths``     存储根目录解析（受 ``FINTRACE_HOME`` 环境变量控制）；
- ``runs``      运行目录与原子写盘（runs/<run_id>/）；
- ``evidence``  只追加的证据日志（runs/<run_id>/evidence.jsonl）；
- ``snapshots`` 外部材料快照与首曝时间判定。

核心链路零第三方依赖：本包只用标准库。
"""
from __future__ import annotations

__all__: list[str] = []
