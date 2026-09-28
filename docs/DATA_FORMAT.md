# 统一数据格式 v1

> 来源：队长 2026-09-25 发在私聊里的截图。
> 原文：「第一版统一数据格式至少包含：研报主张 / 涉及主体和报告期 / 证据文件与页码 /
> 原始数值和单位 / 复算结果 / 核查标签 / 置信度 / 无法判断原因 / 完整运行日志编号。
> 验收标准：其他成员提交的数据能够按同一格式进入系统。」

机器可读定义：`schemas/claim_record.schema.json`
代码模型：`src/fintrace/models.py::ClaimRecord`

---

## 字段对照

| 截图要求 | 字段 | 类型 | 说明 |
|---|---|---|---|
| 研报主张 | `claim_text` | string | 风险信号标题或研报主张原文 |
| 涉及主体 | `subject` | string | 公司全称 |
| 报告期 | `period` | string | `2020A` / `2020Q3` / `2020H1` |
| 证据文件与页码 | `evidence[]` | array | 见下 |
| 原始数值和单位 | `raw_values[]` | array | `metric` / `value` / `unit` / `period` |
| 复算结果 | `recomputation` | object\|null | `formula` / `inputs` / `result` / `error` |
| 核查标签 | `label` | enum | 见 `wording_policy.json` |
| 置信度 | `confidence` | number | 0.0–1.0 |
| 无法判断原因 | `unable_reason` | string\|null | 标签为 `INSUFFICIENT_INFORMATION` 时必填 |
| 完整运行日志编号 | `run_log_id` | string | `FT-YYYYMMDD-<10位哈希>` |

### evidence 条目（同时承载时间过滤四字段）

| 字段 | 说明 |
|---|---|
| `source_id` | 材料编号 |
| `file_name` | 原始文件名 |
| `source_type` | 来源类型枚举，见 schema |
| `publication_date` | **时间过滤字段** `YYYY-MM-DD`；确定不了就留空，会被判为不可用 |
| `page` | 页码 1-based；定位不到写 `null` |
| `quoted_text` | 证据原文片段 |
| `available_before_cutoff` | 由 `cutoff_filter` 计算回填，调用方不用自己算 |

---

## 给其他成员的三条使用约定

1. **只填你确定的东西。** 页码不确定就写 `null`，日期不确定就留空 ——
   系统会把它降级成「当前公开信息不足以确认」，这比填个大概数字安全得多。
2. **数字带单位，别带千分位。** `{"metric":"营业收入","value":150000,"unit":"万元"}`，
   不要写 `"150,000万元"` 或 `1.5亿元`（单位统一由 `calculators` 收敛到「元」）。
3. **同一份材料只出现一个 `source_id`。** 后续所有引用都靠它回指，
   重复或临时的 id 会让时间过滤和页码追溯失效。

最小可用样例：

```json
{
  "claim_id": "CASH_CONVERSION_WEAK",
  "claim_text": "收入现金含量偏低",
  "subject": "示例生态股份有限公司",
  "period": "2020A",
  "evidence": [
    {
      "source_id": "DOC-AR-2020",
      "file_name": "示例生态：2020年年度报告.pdf",
      "source_type": "original_annual_report",
      "publication_date": "2021-04-20",
      "page": 61,
      "quoted_text": "合并现金流量表。销售商品、提供劳务收到的现金 90,000 万元。",
      "available_before_cutoff": true
    }
  ],
  "raw_values": [
    { "metric": "销售商品、提供劳务收到的现金", "value": 90000, "unit": "万元", "period": "2020A" },
    { "metric": "营业收入", "value": 150000, "unit": "万元", "period": "2020A" }
  ],
  "recomputation": {
    "formula": "收现比 = 销售商品、提供劳务收到的现金 / 营业收入",
    "inputs": { "销售商品、提供劳务收到的现金": 900000000.0, "营业收入": 1500000000.0 },
    "result": 0.6,
    "unit": "ratio",
    "error": null
  },
  "label": "RISK_SIGNAL_DETECTED",
  "confidence": 0.55,
  "unable_reason": null,
  "run_log_id": "FT-20260928-dbf4e67bf8"
}
```

---

## 版本策略

- v1 定稿后，**新增字段一律可选**（带默认值），不得修改已有字段的含义。
- 需要改已有字段含义时，升 v2 并保留 v1 解析器至少一个迭代周期，
  否则其他人交上来的数据会静默解析错。
