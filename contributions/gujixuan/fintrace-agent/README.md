# fintrace-agent（FinTrace 独立版）

面向上市公司财报与研报的**可追溯核查 Agent**：自主决定调查路径，事实必须有来源，
关系必须满足适用条件，数值由 Python 复算，最终结论由核验门控制。

对应设计文档：`G:/tools/fintrace-tools/fintrace-独立版方案-v2-2026-10-10.md`（v2.2）。
本仓库**不改动**团队仓库 `G:/项目/fintrace`：确定性内核以「复制 + 记录来源」的方式搬入
（见 `docs/VENDOR.md`），新写的 Agent 层建在它之上。

---

## 快速开始

```bash
cd G:/项目/fintrace-agent
PY=.venv/Scripts/python.exe          # Python 3.12，已装 pytest 与 pymupdf

$PY -m fintrace_agent.cli list                          # 列出案件
$PY -m fintrace_agent.cli doctor                        # 环境自检（不触网）
$PY -m fintrace_agent.cli fixed QM-DRAFT-2023-001       # 固定链路（基线）
$PY -m fintrace_agent.cli agent QM-DRAFT-2023-001       # 自主回路（没配模型时自动回落基线）
$PY -m fintrace_agent.cli gate <run_id>                 # 看某次运行的发布决定
$PY -m fintrace_agent.cli eval                          # 基线口径 + 六项新指标
$PY -m pytest tests -q                                  # 全套测试
$PY tools/run_draft_eval.py                             # 基线口径（必须与团队仓库同数）
$PY tools/check_guardrails.py                           # 反例守门六节
```

---

## 配模型（要跑自主回路才需要）

不配也能跑：`fixed` 模式与全部测试都不需要模型；`agent` 模式没有模型时会**回落固定链路**
并在产物里写明原因，绝不假装成功。

```bash
$PY tools/use_key.py                 # 从 ~/api-key/deepseek/Token.txt 取「金融智能体」那条，写入 .secrets/llm.json
$PY tools/use_key.py --model deepseek-v4-pro    # 想换更强的推理模型
$PY tools/use_key.py --show          # 只看当前配置（掩码）
$PY -m fintrace_agent.cli doctor     # 确认「模型可用：是」
$PY -m fintrace_agent.cli agent QM-DRAFT-2023-001 --max-steps 8
```

* key 存 `.secrets/llm.json`（已 gitignore），**只进后端**：工具只打印掩码，任何时候不要写进代码或对话。
* 该 key 下**只有 `deepseek-flash` 与 `deepseek-v4-pro`**，没有 `deepseek-chat`（写错会被服务端拒）。
* 也可以用环境变量：`FINTRACE_LLM_API_KEY` / `FINTRACE_LLM_BASE_URL` / `FINTRACE_LLM_MODEL`。

---

## 当前状态（先读这段）

| 项 | 状态 |
|---|---|
| 基线口径 | `tools/run_draft_eval.py`：覆盖 1.0000、标签 26/26、TP 12 / FP 0 / FN 0、原件回查 19/19，退出码 0 |
| 测试 | 377 项全绿（`pytest tests -q`，约 60 秒；含真 PDF 的端到端自检，模型用测试替身，离线可复现） |
| 固定链路 | 可用：案件 → 原件回查 → V 门逐条裁决 → `findings.json` / `report.md` |
| 自主回路 | 回路、硬边界、打转守门、四层证据模型、W/V 两道门都已实现并有测试；**真模型（deepseek-flash）已实跑**，卡点从接线转入抽取质量（见 `docs/STATUS.md` 第五节） |
| 知识库 | 从内核抽离成 SQLite（指标 16 / 单位 13 / 公式 1 / 上下文 32 / 关系 9），改动需人审 |
| 联网调查 | 巨潮官方枚举可用（local 模式已验证）；Firecrawl 搜索/抓取为**预留接口**，未配置时明确失败 |
| 自写工具 | 按方案 v2.2 §5.4 **不启用**（前置条件是 OS 级隔离） |

详细模块状态与不足清单见 `docs/STATUS.md`；踩过的环境坑见 `docs/NOTES.md`。

---

## 两条链路

```
① 案件接入     cases/adapter.py        案件 JSON → Document / Claim（相对路径解析成绝对路径 + 内容指纹）
② 固定链路     cases/fixed_runner.py   调搬来的确定性内核：拆草稿 → 逐条核查 → 结论
③ 自主回路     agent/loop.py           硬边界 → Context Builder → 模型选动作 → 工具执行 → W 门 → 落盘
④ 判定         pipeline.py             主张与已核验断言比对（确定性代码；模型不参与数值判定）
⑤ 发布门       gate/finding_gate.py    V1~V7 逐条裁决：引用存在且可回查、关系适用、两轴齐全
⑥ 报告         reporting/report.py     findings.json + report.md（**必须带未解决清单**）
```

模型只做三件事：规划调查路径、语义抽取、必要的定性审查。数值、口径、判定、发布都不在它手里。

---

## 数据模型（四层）

```
Document ──→ Evidence Span ──→ Assertion ──→ Relation
 材料          可定位原文           断言（两轴）      关系（带适用条件）
```

* **两轴**：`evidence_status`（W 门：原文存在、可定位、时间合法）+ `semantic_status`
  （独立检查 B1~B4：概念 / 事件状态 / 统计范围 / 口径）。两轴都过才算事实。
* **适用条件的每个字段**都是 `(主张值, 证据值, 匹配结论)`，`match` 只由程序填 —— 模型填的忽略。
  这是为了挡住「跨分红方案」那类误报（同一份年报里多个方案 / 多个基数 / 多个状态）。
* **精度**：提取阶段原样保存披露小数位，舍入只发生在 `calc_output` 与 `display` 两个具名步骤。

---

## 目录

```
src/fintrace_agent/
├─ contracts.py      统一数据契约（全项目唯一的数据定义处）
├─ paths.py          路径与目录：项目资产跟仓库根，运行时产物跟 FINTRACE_HOME
├─ pipeline.py       端到端编排（fixed / agent 两种模式）
├─ cli.py            命令行
├─ agent/            loop / bounds（硬边界+打转守门）/ planner（上下文）/ model / tool_guard / state
├─ tools/            registry + t_* 工具（含两个预留联网工具与不启用的 t_author_tool）
├─ store/            blobs（内容寻址）/ runs（原子写）/ evidence（只追加）/ snapshots
├─ knowledge/        领域知识库（SQLite）+ 抽取脚本 + 候选更新（人审入册）
├─ facts/            公司事实库（SQLite）
├─ gate/             最终核验门 V + 标签映射
├─ parsers/          格式探测 / docx / 声明式抽取规格 / Span 桥接
├─ reporting/        findings.json + report.md
├─ cases/            案件适配 / 固定链路 / 六项指标
└─ fintrace_engine/  只读搬来的确定性内核（见 docs/VENDOR.md）

data/annotations/    案件、真值、草稿（随仓库）
data/raw/            真实材料（不进版本库）
runs/ blobs/         运行时产物（FINTRACE_HOME）
```

---

## 三条硬约束（与团队仓库一致，不变量）

1. **时间硬门**：`publication_date` 晚于截止日的材料不进检索索引；`historical_replay` 下连**线索**也不进决策上下文（影响注意力就是泄漏）。
2. **措辞守门人**：输出不得出现「虚增收入」「财务造假」这类定性断言，命中即拒绝出报告。
3. **可追溯**：每条结论都要能回到「哪个文件、第几页、哪句话」；定位不到就留空并降级，绝不编页码。

> 本系统只输出风险信号，不做定性结论。报告中任何输出都需经专业人员进一步核查后方可使用。
