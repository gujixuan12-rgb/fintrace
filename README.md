# fintrace

面向上市公司财报与研报的**可追溯风险预警 / 智能核查**系统。

2026 北京市大学生金融人工智能竞赛 · 选题五
「面向上市公司财报与研报的可追溯智能核查系统」

---

## 0. 当前状态（先读这段）

> ⚠️ 本节的"方向"一行在 2026-09-28 核对官方原文后已被标记为**存疑**，
> 详见 `docs/STATUS.md` 第二节第 2 条。在拍板前不要把"事前风险预警"当成定论。

| 项 | 状态 |
|---|---|
| 方向 | **存疑**：2026-09-26 团队定调"事前风险预警"，但官方选题五定义的是"核查给定的研报草稿"，且要求可量化评测 |
| 代码 | 八步主干已打通，用虚构 mock 数据可跑通端到端，23 项测试通过 |
| 真实数据 | 已盘点未接入。6 组真实更正案例已审计，见 `docs/CASE_ANALYSIS.md` |
| 大模型 | **尚未接入**。官方要求"至少使用一个大语言模型作为核心推理引擎"，当前不符合 |
| 统一数据格式 | v1 已定稿（`schemas/claim_record.schema.json`），待其他成员确认 |

**三份必读文档：**

| 文档 | 内容 |
|---|---|
| `docs/STATUS.md` | 当前进度、待拍板事项、下一步动作（会过期，先读这份） |
| `docs/agent-memory/PROJECT_MEMORY.md` | 长期不变量、赛制与官方硬要求、方向演进 |
| `docs/CASE_ANALYSIS.md` | 6 组真实更正案例的可检出性审计（含已复算验证的数字） |

> ⚠️ `data/mock/` 里的公司名与财务数字**全部是虚构的**，只用来打通流程。
> 严禁把 mock 数字当成真实上市公司的事实引用到报告或答辩材料里。

---

## 1. 核心流程（八步）

对应队长 2026-09-26 的要求「设计 Agent 工作流」：

```
① 文件识别          parsers/document_recognizer.py
② 截止日期过滤  ←硬门  verification/cutoff_filter.py
③ 数据提取          retrieval/evidence_index.py
④ 确定性计算        calculators/ratios.py
⑤ 异常评分          verification/anomaly_rules.py
⑥ 风险假设          verification/anomaly_rules.py
⑦ 核查建议          verification/anomaly_rules.py
⑧ 证据引用          reporting/risk_report.py
```

编排在 `orchestrator.py::run()`，一次运行同时产出：
- `records` —— 统一数据格式 v1 的 `ClaimRecord` 列表（给其他成员 / 前端）
- `report` —— 结构化风险报告（给人看，已过措辞约束）

---

## 2. 三条硬约束

### 2.1 时间过滤（代码级硬门，不是提示词）

每条证据必须带 `publication_date` / `source_type`，系统计算 `available_before_cutoff`。
**`publication_date` 晚于 `prediction_cutoff_date` 的材料，根本不会进入检索索引**，
模型没有机会把它写进报告。测试 `test_cutoff_excludes_late_document` 守着这条。

### 2.2 措辞约束（代码级守门人）

`verification/wording_guard.py`。允许输出：

- 存在风险信号
- 建议进一步核查
- 可能涉及某类风险
- 当前公开信息不足以确认

禁止输出「公司虚增收入」「公司财务造假」「供应商交易虚假」这类**定性断言**。
唯一例外：输入中已存在监管或公司正式确认文件，**且**任务模式为 `post_hoc`。

命中黑名单直接抛 `WordingViolation`，**不生成半成品报告**。
策略口径同时写在 `schemas/wording_policy.json`，给人和前端看同一份。

### 2.3 可追溯（页码，不许编）

每条信号必须能回溯到「哪个文件、第几页、哪句话」。
定位不到时：`page = null`，标签降级为 `INSUFFICIENT_INFORMATION`，置信度显著下调，
并在 `unable_reason` 写明原因 —— **绝不编造一个看起来合理的页码**。

---

## 3. 快速开始

```bash
cd G:/项目/fintrace
export PYTHONPATH=src          # Windows git-bash；PowerShell 用 $env:PYTHONPATH="src"

python -m fintrace.cli demo                          # 用内置 mock 跑一遍
python -m fintrace.cli run data/mock/mock_original_inputs.json --out out/report.json
python -m fintrace.cli guard "经核查，公司虚增收入 2 亿元"   # 试措辞守门人（会被拒绝）

python -m pytest tests -q                             # 23 项
```

零第三方依赖（除测试可选装 `jsonschema`）。接真实 PDF 时需要 `pymupdf`。

---

## 4. 目录

```
fintrace/
├─ data/
│  ├─ raw/            真实材料（不进 git，见 .gitignore）
│  ├─ processed/      结构化中间产物
│  ├─ annotations/    人工标注（错误标签 / 风险点）
│  └─ mock/           虚构样例，用于打通流程与回归
├─ schemas/
│  ├─ claim_record.schema.json   统一数据格式 v1
│  └─ wording_policy.json        措辞约束口径
├─ src/fintrace/
│  ├─ models.py
│  ├─ orchestrator.py
│  ├─ cli.py
│  ├─ parsers/        ①
│  ├─ retrieval/      ③⑤
│  ├─ calculators/    ④
│  ├─ verification/   ②⑥⑦ + 措辞守门人
│  └─ reporting/      ⑧
├─ tests/
└─ docs/
```

> 说明：队长的目录树里 `src/` 下直接是 `parsers/ retrieval/ calculators/ verification/ reporting/`。
> 这里多包了一层 `src/fintrace/`，是为了让包能正常 import 和打包（Python 标准做法），
> 五个子模块的划分与队长给的完全一致。

---

## 5. 分工

| 成员 | 负责 |
|---|---|
| 陈翊民 | 队长 / 建模 / 总体方案 |
| 谷纪瑄 | **Agent 工作流与工程骨架**（本仓库） |
| 齐飞扬 | 编程 / 案例数据收集 |
| 陈丹阳 | 数据清洗与分析 / 写作 |
| 陈颖孜 | 财报行研 / 风险信号梳理 / 报告撰写 |

**其他成员提交的数据怎么进来**：按 `schemas/claim_record.schema.json` 产 JSON，
`ClaimRecord.from_dict()` 直接吃 —— 验收标准就是「其他成员提交的数据能够按同一格式进入系统」。

---

## 6. 免责边界

本系统只输出**风险信号**，不做定性结论。
报告固定附带免责声明，任何输出都必须经专业人员进一步核查后方可使用。
