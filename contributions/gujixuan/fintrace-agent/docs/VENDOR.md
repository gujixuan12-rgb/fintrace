# 内核搬运记录（VENDOR）

来源：`G:/项目/fintrace`　HEAD `f43418b5001ff3d5b6d97cff298c8c43c1946670`（2026-10-08 17:22:38 +0800）
搬运日期：2026-10-10

**规矩：搬进来的 `src/fintrace_engine/` 是只读的。** 独立版的任何改动都不许写回内核，
需要改判定口径就写在 `src/fintrace_agent/` 里包一层。团队仓库那边一行不动。

## 搬了什么

| 独立版路径 | 团队仓库来源 | 改动 |
|---|---|---|
| `src/fintrace_engine/`（32 个 .py） | `src/fintrace/` | 包名 `fintrace` → `fintrace_engine`；目录深度保持 `parents[2]`，路径解析不变 |
| `tools/run_draft_eval.py` | `tools/run_draft_eval.py` | 导入前缀改为 `fintrace_engine.` |
| `tools/check_guardrails.py` | `tools/check_guardrails.py` | 同上 |
| `data/annotations/`（案件、真值、草稿、评测标注） | `data/annotations/` | 原样 |
| `data/raw/002232/`、`data/raw/600556/`（真实材料 PDF） | `data/raw/` 对应目录 | 原样；不进版本库（`.gitignore`） |
| `schemas/` | `schemas/` | 原样 |

## 搬运后的锚点

搬完立刻跑基线，读数必须与团队仓库**同数**，对不上就是搬坏了：

```
覆盖 1.0000 ｜ 标签 26/26 ｜ 内容断言 12/12 ｜ 原件回查 19/19 ｜ exit 0
```

（实测日期 2026-10-10，两边一致。）

## 将来怎么同步

团队仓库若继续演进，不要直接覆盖搬运。做法：先跑上面的基线读数，确认新 commit
的读数与本文记录一致或有明确变化，再按上表逐个文件替换，最后重跑基线与
`pytest tests`（独立版已有的 357 项必须全绿）。搬运内容一旦被独立版代码依赖，
就在本文件追加一行说明依赖点。
