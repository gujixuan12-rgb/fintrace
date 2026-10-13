"""领域知识库（规则层）—— 模块 B。

四个公共部件：
- ``extract_from_engine`` —— 一次性生成脚本，从只读内核 ``fintrace_engine`` 运行时抽取
  指标 / 单位 / 精度 / 公式，落成 ``knowledge/data/*.yaml``（不手抄数字）。
- ``build`` —— 把 YAML 编译进 SQLite（``knowledge/knowledge.sqlite``）。
- ``loader`` —— 运行时只读查询接口 ``KnowledgeBase``。
- ``proposals`` —— 知识更新三件套：``propose``（不写库）/ ``verify_independent`` / ``ratify``
  （唯一写权威库的入口）。

数据文件格式见 ``yamlio``：标准库没有 YAML，这里用一个受控的极小子集（详见该模块说明）。
"""
