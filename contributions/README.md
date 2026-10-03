# 齐飞扬交付索引

同一项目的两个案例，分别存放。启明用于当前第一条演示，美晨作为复杂案例保留。

当前上传的是这个名为 `contributions` 的文件夹本身，仓库路径对应 `contributions/meichen_2018_2020/`、`contributions/qiming_2023/` 和 `contributions/验收记录/`
| | 美晨生态 | 启明信息 |
|---|---|---|
| 目录 | [meichen_2018_2020](meichen_2018_2020/README.md) | [qiming_2023](qiming_2023/README.md) |
| 报告范围 | 2018—2020年原始年报 | 2023年原始年报，第51、223页 |
| 截止日 | 2021-04-30 | 2024-04-22 |
| 原始数据 | `original_inputs.csv`、`original_inputs.json` | `qiming_2023_dividend.json` |
| 待核查主张 | 此次没有单独提供 | `claim_input.json` |
| PDF读取程序 | 此次交付以整理好的数据为主 | `read_evidence.py` |
| 测试与标准答案 | [美晨数据验证报告](验收记录/美晨生态/validation_report.json) | `tests/`、`evaluation/` |

## 按用途找文件

- **拿数据**：美晨使用 `meichen_2018_2020/original_inputs.json`；启明使用 `qiming_2023/qiming_2023_dividend.json`。CSV是美晨同一套数据的人工检查表，不是另一份独立来源。
- **拿测试主张**：使用 `qiming_2023/claim_input.json`。这是根据原始年报编写的测试句子，不是真实券商研报摘录。
- **读取启明PDF**：看 `qiming_2023/README.md`，运行 `read_evidence.py`。保留现有目录层级及 `evaluation/` 内文件相对位置。
- **查看正确答案**：只有独立评测时读取 `qiming_2023/evaluation/`；检测或Agent输入不包含该目录。
- **查看实际验收记录**：见[验收记录说明](验收记录/README.md)。本地运行记录已经随本目录一起提供，供复核查看，不作为核查输入。
- **查原始PDF**：在完整交付包的 `03_原始PDF_不上传GitHub/`，不包含在GitHub上传目录里。

## 接口说明

美晨JSON沿用 `as_of_date / sources / financials` 等结构；启明JSON沿用队长提供的 `prediction_cutoff_date / pre_cutoff_source / observations` 结构。两份数据不能直接作为同一接口互换，统一接入时需分别映射。

美晨详细说明中的计算示例对应 `contributions/chen_yimin` 独立计算包；启明读取脚本调用根工程的截止日过滤器。按照HANDOFF要求将两个同名 `fintrace` 包分进程运行，不要混用导入路径。

启明输出的 `SUPPORTED` 是独立正确主张检查的结果，不是对根工程标签枚举的修改。研报主张拆解、大模型调用及统一报告由主工程集成。

本目录从原 `qi_feiyang` 改名并作为单层 `contributions` 上传后，启明脚本默认按固定层数寻找仓库的方式不适用；请在仓库根执行README中的命令，保留 `--repo-root .` 显式指定根工程。本次只改路径说明和随包记录，没有修改计算、输入数据或程序逻辑。

本目录仅汇集已交付成果。原始数据、争议项保留方式和截止日均未因目录整理而改变。
