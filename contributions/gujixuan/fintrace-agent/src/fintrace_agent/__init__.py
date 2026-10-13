"""FinTrace 独立版 —— 受约束的核查 Agent。

分层（v2.2）：
    contracts.py   统一数据契约，全项目唯一的数据定义处
    agent/         回路：硬边界 → 上下文 → 决策 → 执行 → W 门 → 落盘
    tools/         工具层：六动作 + 检索 + 联网线索 + 声明式抽取
    store/         原文层：blob / 运行目录 / 证据日志 / 快照
    knowledge/     规则层：概念、公式、关系、适用条件（写入需人审）
    facts/         实例层：已核验断言与关系
    gate/          最终核验门：逐条 Finding 的发布控制
    parsers/       材料格式探测、docx、声明式抽取、Span 桥接
    reporting/     findings.json / report.md
    fintrace_engine/  只读搬来的确定性内核（fixed 模式基线，见 docs/VENDOR.md）
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
