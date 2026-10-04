# 启明信息原始证据与页面读取交付

齐飞扬分工对应的补充材料，核验日期：2026-10-04。

已补齐原始年报的官方披露入口、日期、哈希、页码，提供可运行的两页 PDF 读取脚本，以及一条有原始证据支持的正确主张。沿用队长输入中的截止日 **2024-04-22**，保留其四项原始数值及字段名称。

## 已核验来源

- 公司：启明信息技术股份有限公司，002232。
- 文件：2023年年度报告全文（原始版本）。
- [巨潮资讯公告详情](https://www.cninfo.com.cn/new/disclosure/detail?stockCode=002232&announcementId=1219596126)；[官方 PDF](https://static.cninfo.com.cn/finalpage/2024-04-13/1219596126.PDF)。
- 正式披露日期：**2024-04-13**。核验公告详情页实际显示的公司、标题、日期以及内嵌 PDF 地址；日期精度为日，未声称核验首次披露的具体时刻。
- 文件大小：4,369,829 字节；237 页。
- SHA-256：`a9f276a963e3233aad87ef3eec860af96e99c0819f6ceebcde39df72468ea0d1`。
- 材料来自本次收到的六组案例文件。以上是对所提供文件的来源核验，不声称本交付作者是案例包最初收集者。

| 原始字段 | 披露值 | 单位 | PDF页序（从1开始） | 印刷页码 |
|---|---:|---|---:|---:|
| shares | 408548455 | 股 | 51 | 51 |
| dividend_per_ten_shares | 0.10 | 元/10股，含税 | 51 | 51 |
| disclosed_dividend | 408548.46 | 元，含税 | 51 | 51 |
| other_page_total | 408.55 | 万元 | 223 | 223 |

这些数值属于年报中的利润分配预案，股本基准日为2024-04-12；不代表2023年已经实际付款。属于上市公司向股东分配的口径，不是合并利润表或母公司利润表的损益项目。第四项的含税口径来自同一段“每10股派发现金红利0.10元（含税）”的方案说明。

第51页与第223页的金额分别保留，不相互替换；是否与复算值一致应由后续计算模块判断。本工具只负责忠实读取，及检查下面这一条每10股派息主张。

## 正确主张样本

> 启明信息在2023年年度报告披露的利润分配预案为每10股派发现金红利0.10元（含税）。

这是依据年报编写的测试主张，不是实际券商研报摘录。第51页预案表和第223页方案段落支持它。配套确定性检查返回 `SUPPORTED`，没有把同报告其他金额的差异传导为此条主张的错误。

`SUPPORTED` 是此独立检查的结果码，不是对根工程 `ClaimRecord.label` 枚举的修改。主流程的标签映射由集成人员根据当前统一接口处理。

## 文件与读取边界

| 文件 | 用途 | 可否作为核查输入 |
|---|---|---|
| qiming_2023_dividend.json | 沿用队长输入的四项原值，补官方来源、日期、单位、口径、原文及页码 | 可以 |
| claim_input.json | 单条待核查主张，不带标准答案 | 可以 |
| read_evidence.py | 先过根工程截止日过滤，再验哈希、读两页并核对摘录 | 程序 |
| evaluation/expected_labels.json | 正确主张的独立标准答案 | 不可以 |
| evaluation/captain_post_cutoff_validation.json | 队长原输入中后续验证字段的独立副本 | 不可以 |
| evaluation/source_date_audit.json | 完整来源核验记录，包含后续公告的日期元信息 | 不可以 |
| evaluation/evaluate.py | 检测结束后独立读取结果和标准答案 | 评测程序 |

队长原输入混有 `post_cutoff_validation` 字段，本交付把它移到 `evaluation/`；队长原附件未被改写。本次只核验后续公告在官方列表中的标题和日期，未再次读取其正文，其更正后金额仍明确标为队长提供的答案。

核查或 Agent 只接收明确指定的输入文件和获准原始 PDF 页面，不要递归加载整个交付目录。`evaluation/` 不进入证据检索库。字段拒绝检查只是辅助措施；真正的隔离是检测进程不打开答案文件。

## 在仓库中运行

按当前改名后的上传目录，本文件位于仓库根目录下的 `contributions/qiming_2023/`。原始 PDF 单独放在本地 `data/raw/qiming/`，**不提交到 Git**。在仓库根目录、Python 3.10 及以上环境中运行；保留命令中的 `--repo-root .`，不使用旧层级的默认推断：

```powershell
python -m pip install -r contributions/qiming_2023/requirements.txt
python contributions/qiming_2023/read_evidence.py --repo-root . --pdf "data/raw/qiming/01_改错前_2023年年度报告.pdf" --out-dir "contributions/qiming_2023/out"
```

PDF 可直接使用六组案例中“02_启明信息_002232_分红计算/01_改错前_2023年年度报告.pdf”，通过 `--pdf` 传实际路径即可，无需改文件名。脚本本身不下载材料，不会根据文件名猜版本。

检测输出为 `out/evidence_reading.json`：包含四项原始数字、单位、口径、实际抽取原文、双页码、日期、哈希、根工程 `EvidenceRef` 可直接构造的引用、主张检查结果和调用记录。浮点金额用十进制字符串保存，比较用 `Decimal`。

`--repo-root` 必须指向根工程；不要指向 `contributions/chen_yimin`。本脚本没有另建同名 `fintrace` 包；它调用根工程的 `verification/cutoff_filter.py`，不调用独立计算包。日期缺失、非法或晚于截止日时不读取 PDF；哈希或来源版本不符时不解析正文；单位或原始摘录对不上时不输出可用证据。

程序为核验哈希需要读取整个 PDF 的二进制数据，但只抽取第51、223页正文。日志中的 `pdf_content_read` 表示是否开始页面正文抽取，`pdf_pages_read` 记录已尝试访问的页序。发布日期来自人工完成的官方页核验；不把这一过程声称为自动公告抓取。

检测结束后再运行评测：

```powershell
python contributions/qiming_2023/evaluation/evaluate.py --result "contributions/qiming_2023/out/evidence_reading.json" --out "contributions/qiming_2023/out/acceptance.json"
```

成功时检测退出码为0，结果为 `EVIDENCE_READY`、主张为 `SUPPORTED`；评测 `passed` 为 `true`。这是一次结构化主张的确定性检查，不等于整套研报核查、大模型主张拆解或前端已接通。

## 验证与接入

本地真实 PDF 已在队长提供分支的根工程、此前启明接入版本分别运行通过。独立评测确认正确主张未被误报。附带测试覆盖截止日、文件哈希、原始版本、页码和字段唯一性、单位、主张预案语义以及失败日志；真实 PDF 测试由 `FINTRACE_QIMING_PDF` 指向本地文件启用。

既有运行记录及此次改名后按显式根路径运行的复验，见[验收记录](../验收记录/README.md)。它们是核查输出和复核材料，不进入Agent的输入或证据检索库。

```powershell
python -m pytest tests -q
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
$env:FINTRACE_QIMING_PDF = (Resolve-Path 'data/raw/qiming/01_改错前_2023年年度报告.pdf').Path
python -m pytest contributions/qiming_2023/tests -q
```

集成人员可取 `observations` 给确定性计算模块，取 `evidence_refs` 对接根工程 `EvidenceRef`，取 `page_excerpts` 给主张核查模块。不要把已经含 `claim_check` 的完整检测结果重新作为同一次模型评测输入；模型验证应读取原始输入及证据片段。

此次交付未改写根工程模型、计算规则、标签枚举或其他成员代码。完整接入研报主张拆解、LLM、统一报告及演示界面，仍由技术集成分工完成。
