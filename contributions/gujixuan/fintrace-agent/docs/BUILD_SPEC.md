# FinTrace 独立版 · 逐模块施工说明书

对应设计文档：`G:/tools/fintrace-tools/fintrace-独立版方案-v2-2026-10-10.md`（v2.2）。
本文件是施工口径：**每个模块的公共接口、不变量、必测项、验收命令**。模块之间只通过
`src/fintrace_agent/contracts.py` 的数据类通信，不得互相 import 私有函数。

---

## 0. 全局规矩（每个模块都适用）

1. **解释器**：`G:/项目/fintrace-agent/.venv/Scripts/python.exe`（Python 3.12，已装 pytest 9.1.1、pymupdf 1.28.2）。
   工作目录永远是 `G:/项目/fintrace-agent`。测试命令：`.venv/Scripts/python.exe -m pytest tests/<你的目录> -q`
   （`pyproject.toml` 已配 `pythonpath = ["src"]`，不需要设 PYTHONPATH）。
2. **核心链路零第三方依赖**：只能用标准库。读 PDF 才允许 `pymupdf`（延迟 import，缺了就降级）。
   不要新增任何依赖，不要 pip install 任何东西。
3. **不要改这些文件**（主代理所有）：`src/fintrace_agent/contracts.py`、`pyproject.toml`、`docs/BUILD_SPEC.md`、
   `tools/run_draft_eval.py`、`tools/check_guardrails.py`。缺字段就写进你的 `extra` 字典并回报，不要动契约。
4. **`src/fintrace_engine/` 是只读搬运的内核**（团队仓库 `f43418b` 原样抄来，见 `docs/VENDOR.md`）。
   可以 import 使用，绝不修改、不删除。它是「已实现的确定性零件」，也是 fixed 模式的基线引擎。
5. **不联网跑测试**。需要网络的工具在测试里只验证「未配置时明确失败」。
6. **注释与文档字符串用简体中文**（不要繁体）；代码标识符用英文。
7. **不许为了让测试变绿而放宽断言**。测试红着就报红，并在回报里写清楚。
8. 每个模块交付时回报：文件清单、公共接口、跑过的测试命令与真实输出（通过数/失败数）、
   你**没能做到**或**存疑**的地方（这一项必填，不许写「无」了事——写「确实没发现」也要写）。

---

## 1. `store/` —— 原文层与落盘（主人：模块 A）

### `store/blobs.py`（内容寻址存储）
```python
def put_bytes(data: bytes) -> str          # 返回 sha256 十六进制
def put_text(text: str) -> str
def get(sha: str) -> bytes                 # 不存在抛 KeyError
def get_text(sha: str) -> str
def path_for(sha: str) -> Path             # blobs/<前两位>/<sha>.bin
def exists(sha: str) -> bool
```
不变量：同内容同 hash；同 hash 不重复落盘；写入必须原子（临时文件 + `os.replace`）。

### `store/runs.py`（运行目录）
```python
def new_run_id(case_id: str = "") -> str                  # 形如 FT-20261010-a1b2c3d4
def run_dir(run_id: str, create: bool = False) -> Path    # runs/<run_id>/
def atomic_write_json(path: Path, obj) -> None            # 临时文件 + os.replace，写完可读
def read_json(path: Path) -> dict                         # 不存在返回 {}
```
运行目录固定文件（v2.2 §3.5）：`state.json`、`findings.json`、`evidence.jsonl`、`decisions.jsonl`、
`rejections.jsonl`、`report.md`、`snapshots/`、`tools/`。

### `store/evidence.py`（证据日志，只追加）
```python
class EvidenceLog:
    def __init__(self, run_id: str)
    def add_spans(self, spans: list[Span]) -> list[str]        # 返回 span_id 列表
    def add_assertions(self, items: list[Assertion]) -> list[str]
    def spans(self) -> list[Span]
    def assertions(self) -> list[Assertion]
    def span(self, span_id: str) -> Span | None
    def assertion(self, assertion_id: str) -> Assertion | None
    def index(self) -> list[str]                                # 证据索引（只 id，不正文）
    def blob_put(self, text: str) -> str                        # 大文本走 blobs
```
不变量：只追加，绝不覆盖或删除（原文留档是红线）；`span_id`/`assertion_id` 全局唯一。

### `store/snapshots.py`（外部材料快照）
```python
def record(url: str, text: str, *, meta: dict | None = None,
           first_published_at: str = "", version: str = "") -> dict
    # 返回 {"snapshot_hash", "retrieved_at", "path", "first_published_at", "version"}
def load(snapshot_hash: str) -> dict          # {"text":..., "meta":...}
def before_cutoff(snapshot: dict, cutoff: str) -> bool   # 用 first_published_at 判，不是 retrieved_at
```
不变量：**时间判定一律用首次公开时间**（v2.2 §5.2 L3）；没有首次公开时间的快照一律判为「不可用」。

**模块 A 必测**：blob 幂等与原子写；EvidenceLog 只追加、断电式重读一致；`before_cutoff` 对
「抓取时间早于 cutoff 但首曝晚于 cutoff」必须返回 False（这是 L3 的关键反例）。

---

## 2. `knowledge/` —— 领域知识库（规则层，主人：模块 B）

### `knowledge/extract_from_engine.py`（一次性生成脚本）
从 `fintrace_engine.draft_review` 抽出：`METRICS`、`METRIC_KEY_ALIASES`、`MONEY_SCALE`、
`COUNT_SCALE`、`PASSTHROUGH_UNITS`、`PRECISION`，以及 `calculators/consistency.py` 的公式定义，
生成 `knowledge/data/{concepts,units,formulas,contexts,relations}.yaml`。
做法：**运行时读引擎对象再落 YAML**（不要手抄数字，避免抄错）。幂等：重复跑结果一致。

### `knowledge/build.py`
```python
def build_db(yaml_dir: Path, db_path: Path) -> dict   # 返回各表行数
```
SQLite 表：`concepts(metric_id, cn, aliases_json, base_unit, domain, caliber_hint, version, status)`、
`units(unit, base, scale, precision, rounding)`、`formulas(formula_id, metric_id, caliber, expression,
inputs_json, rounding, applicable_when, rule_version, status)`、
`contexts(context_id, kind, value, note)`、`relations(rule_id, predicate, subject_metric, object_metric,
applicable_when, rule_version, status)`。
`status` ∈ `authoritative` / `draft`（草稿规则只在本 run 生效，见 v2.2 §6.4）。

### `knowledge/loader.py`
```python
class KnowledgeBase:
    def __init__(self, db_path: Path)
    def resolve_metric(self, name_or_alias: str, anchor: int | None = None) -> dict | None
    def formula(self, metric_id: str, caliber: str = "") -> dict | None
    def relation_rule(self, rule_id: str) -> dict | None
    def context_check(self, entity: str = "", period: str = "", unit: str = "") -> dict
    def precision_for(self, metric_id: str, unit: str) -> dict | None
    def all_metrics(self) -> list[dict]
```
不变量：`resolve_metric` 对引擎 `METRICS` 里每个 `cn` 与每个别名都必须能命中（覆盖测试要断言
「全部命中」，不是抽样）；返回值里带 `caliber` 与 `base_unit`。

### `knowledge/proposals.py`
```python
def propose(kb, *, kind: str, entry: dict, run_id: str, evidence: list[str]) -> dict
def verify_independent(proposal: dict, other_cases: list[str], runner) -> dict
def ratify(kb, proposal: dict, approver: str) -> dict     # 唯一写权威库的入口
```
不变量（v2.2 §8）：`propose` **不写库**；写权威库只能经 `ratify`；`ratify` 必须写
`previous_version`、`valid_from`、`approver`、`introduced_by_run`。

**模块 B 必测**：全量指标命中；别名命中；`propose` 不改库（前后行数一致）；`ratify` 后
版本递增且 `previous_version` 正确；draft 状态条目不被权威查询返回。

---

## 3. `facts/` —— 公司事实库（实例层，主人：模块 C）

### `facts/store.py`
```python
class FactStore:
    def __init__(self, db_path: Path)
    def put_assertion(self, a: Assertion) -> str            # 返回 assertion_id，按 id 幂等
    def put_relation(self, r: Relation) -> str
    def get_assertion(self, assertion_id: str) -> Assertion | None
    def get_relation(self, relation_id: str) -> Relation | None
    def query(self, *, subject: str = "", period: str = "", metric: str = "",
              only_facts: bool = True, kind: str = "") -> list[Assertion]
    def relations_for(self, assertion_id: str) -> list[Relation]
    def cross_case_rule_usage(self, rule_id: str) -> list[str]   # 跨案件统计（换 PG 的触发口）
    def stats(self) -> dict
```
不变量：`only_facts=True` 只返回 `evidence_status=attested` **且** `semantic_status=bound`；
两条轴都不满足的**仍要落库**（留痕），只是查不出来当事实用；同一 id 重复写入必须幂等。

**模块 C 必测**：幂等；`only_facts` 过滤掉 candidate/unbound；`query` 的三元组索引命中；
`cross_case_rule_usage` 跨两个 run 统计；进程重启后数据仍在。

---

## 4. `parsers/` —— 材料格式与抽取（主人：模块 D）

### `parsers/format_probe.py`
```python
def probe(path: str | Path) -> dict   # {"format": MaterialFormat 值, "why": "...", "signature": "..."}
```
按魔数判：`%PDF-` → pdf；`PK\x03\x04` 且含 `word/document.xml` → docx；`D0 CF 11 E0` → doc；
纯文本可解码 → text；否则 unknown。**探测不出返回 `unknown`，不许当空材料。**

### `parsers/docx_extract.py`
```python
def extract_spans(path: str | Path, doc_id: str, *, start_span: int = 0) -> list[Span]
```
只用 `zipfile` + `xml.etree.ElementTree`：读 `word/document.xml`，按段落与表格行产出 Span
（表格行取 `w:tr`，单元格文本按 `w:tc` 拼接，`span_kind="table_row"`，`bound_context.row_label`
取该行第一个单元格文本）。`char_range` 用该段在文档 XML 文本流中的偏移（近似即可，必须单调）。

### `parsers/spec_extract.py`（声明式抽取规格，v2.2 §5.4 的过渡路径）
```python
def validate_spec(spec: dict) -> list[str]          # 返回错误列表，空即通过
def run_spec(spec: dict, pages: list[dict]) -> list[Span]
    # pages: [{"page": int, "text": str}]
def load_spec(path: str | Path) -> dict
```
规格结构见 v2.2 §5.4。算子白名单严格限定：`locate.kind ∈ {paragraph, table_row,
regex_in_paragraph}`、`extract.kind ∈ {text, group}`、`group.index` 必须是整数。
**必须拒绝**：未知算子、`pattern` 里出现除正则字面之外的可执行痕迹、字段名不在白名单
（`claim_text/value/unit/period/metric/row_label/page/quote`）、任何 `import`/`exec`/`open` 字样。
校验失败的规格不得执行。

### `parsers/bridge.py`
```python
def text_to_spans(pages: list[dict], doc_id: str, *, span_kind: str = "paragraph",
                  extractor: str = "builtin") -> list[Span]
def decimals_of(raw: str) -> int | None     # "408,548.46"→2；"0.155"→3；"12"→0；"约 1,000 万元"→None
```
`decimals_of` 是**精度保真**的第一道关口（W6 依赖它）：只认纯数字/千分位数字串，
4,085,484.55 → 2；带中文、带单位、带「约」一律返回 None（宁可 None，不许截断）。

**模块 D 必测**：探测四类魔数各一例（docx 用 `zipfile` 现场造一个最小 docx）；
`validate_spec` 拒绝未知算子与危险字样；`run_spec` 抽到一个值并保留 `raw_text`；
`decimals_of` 的 `0.155→3` 与「带单位→None」两个关键断言。

---

## 5. `tools/semantic_binding.py` + `tools/validator.py` —— W 门与语义绑定（主人：模块 E）

### `tools/semantic_binding.py`
```python
@dataclass
class BindingOutcome:
    assertion: Assertion
    checks: list[dict]        # [{"code":"B1","ok":bool,"why":str}]
    semantic_status: str

def bind(assertion: Assertion, *, spans: list[Span], kb, fact_store=None,
         llm_verdict: str | None = None) -> BindingOutcome
```
四项检查：
- **B1 概念绑定**：`kb.resolve_metric(assertion.metric)` 命中才算过（别名、简写、同义）；
  命中失败 → `unbound`，命中但值域/单位与概念定义冲突 → `conflicted`。
- **B2 事件状态与时态**：`已实施 / 已审议通过 / 预案 / 计划 / 预测` 五类，`text` 里出现的状态
  与 `period`/`origin` 必须一致；预测类（`kind=forecast`）不得标成已实施。
- **B3 统计范围**：`单一方案 / 本报告期合计 / 多年累计`，从 `bound_context.row_label` 与
  `text` 判定；判定不出 → `unbound`（不许默认成单一方案）。
- **B4 口径**：`合并 / 母公司 / 扣非`，`bound_context` 与 `text` 冲突 → `conflicted`。
**权限不对称（v2.2 §7）**：`llm_verdict` 只允许把结果**拉低**（`bound` → `conflicted`），
不允许把 `unbound`/`conflicted` 抬成 `bound`。**必须有一条测试专门钉这个不等号。**

### `tools/validator.py`（W1~W6）
```python
@dataclass
class Admission:
    accepted: list[Assertion]
    rejected: list[dict]        # {"code":"W2","span_sha"/"raw":..., "why":...}
    checks: dict                # {"W1":n,...}

class Validator:
    def __init__(self, *, kb, cutoff_date: str, page_texts: Callable[[str, int | None], str | None],
                 allowed_source_types: set[str] | None = None)
    def admit(self, candidates: list[Assertion], *, spans: list[Span], doc: Document) -> Admission
```
- W1 格式：必填字段齐全、类型正确，`raw_text`/`value` 能解析。
- W2 来源：`doc.publication_date <= cutoff_date`（硬门，失败即 rejected，且**不许进事实库**）；
  来源类型在允许集合内；页码或公告号存在。
- W3 身份：证券代码/报告期/是否摘要或更正版与案件一致（更正版在 `historical_replay` 下直接拒）。
- W4 数值五联：数值按位、单位按换算比、符号同向、指标同名、页码与该句的数联合绑定；
  逐项写出在哪一条上失败。
- W5 归置：过 → `evidence_status=attested`；不过 → rejected 明细（**不是静默丢**）。
- W6 精度保真：`decimals_of(raw_text)` 与 `decimals_as_disclosed` 一致；若解析值小数位少于披露位
  （截断痕迹）→ rejected。

**模块 E 必测**：W2 时间违规（`publication_date` 晚于 cutoff 一天即被拒）；W4 单位不符；
W6 截断（`0.155` 被当成 `0.15` 必须被抓）；B 系列四项各一正一反；`llm_verdict` 不等号；
`admit` 的 accepted 里所有条目的 `evidence_status == attested`。

---

## 6. `gate/` —— 最终核验门 V（主人：模块 F）

### `gate/labels.py`
```python
def report_labels(verdict: str, *, authoritative_doc: bool = False,
                  mode: str = "pre_hoc") -> list[str]     # 用 contracts.LABEL_MAP
```
映射规则见 v2.2 §2.6：`CONTRADICTED → RISK_SIGNAL_DETECTED + FURTHER_VERIFICATION_SUGGESTED`；
`SUPPORTED` 不产风险标签；`INSUFFICIENT → INSUFFICIENT_INFORMATION`；`PREDICTION` 不产风险标签；
仅有监管/正式确认文件且 `mode == post_hoc` 时给 `CONFIRMED_BY_AUTHORITATIVE_DOCUMENT`。

### 引用契约（V3/V5 的检查对象，必须显式填）

`Finding.assertion_refs` / `Finding.relation_refs` 是 V3/V5 唯一权威的引用来源。
历史兼容：`finding.audit` / `finding.correction` 里的 `assertions|assertion_ids|evidence_ids`、
`relations|relation_refs|relation`，以及 `finding.evidence[*].assertion_id` 仍会被读，
但**新代码一律填显式字段**。

为什么要有 `require_refs` 开关：V 门若读不到引用就查不出问题，会一路放行（静默漏检）。
agent 模式建门时置 `require_refs=True` —— 一条 Finding 自称有材料支撑却零断言引用，
降级为待人工复核；fixed 基线（引擎直出，没有两轴模型）置 False。

### `gate/finding_gate.py`
```python
class FindingGate:
    def __init__(self, *, evidence, kb, state, page_texts, assertions_by_id: dict,
                 verifier=None, mode: str = "pre_hoc")
    def review(self, findings: list[Finding]) -> GateResult
```
```python
@dataclass
class GateResult:
    findings: list[Finding]        # 已填 audit / publish / block_reasons / report_labels
    residuals: list[Residual]
    counts: dict                   # {"allowed":n,"blocked":n,"manual_review":n}
```
V1 逐条 Finding 结构完整；V2 **回查原件**（`page_texts(file_name, page)` 取原文，值/引文对不上即
blocked，取不到原文 → `manual_review` 并记 residual）；V3 关系适用性（引用 Relation 必须
`applicable`，否则 blocked；`unknown` → manual_review）；V4 Task State（未处理关键疑点、
失败工具调用、超授权、未完成步骤 → 记 residual）；V5 语义绑定（引用的 Assertion 必须
`is_fact`，否则不得 `allowed`）；V6 残余清单（只要有 `residuals` 就必须出现在结果里）；
V7 发布决定（任一阻断 → blocked / manual_review）。

**模块 F 必测**：值不在页 → blocked；值在页但引用未绑定 Assertion → manual_review；
全绿 → allowed 且 `report_labels` 含 `RISK_SIGNAL_DETECTED`；有残余项时 `residuals` 非空；
`SUPPORTED` 不产生风险标签。

---

## 7. `tools/` —— 工具层（主人：模块 G）

### `tools/registry.py`
```python
@dataclass
class ToolSpec:
    name: str
    params: dict                 # JSONSchema 片段
    receipt: dict
    preconditions: list[str]     # 人类可读，判定逻辑放 tool_guard
    timeout_s: int = 20
    enabled: bool = True
    reserved: bool = False       # True = 预留接口，未配置时明确失败

def register(spec: ToolSpec) -> None
def get(name: str) -> ToolSpec | None
def all_specs() -> list[ToolSpec]
def execute(name: str, args: dict, ctx) -> ToolReceipt
```
`execute` 必须：未注册/未启用 → `ToolReceipt(ok=False, error="unknown_tool"/"disabled")`；
计时 `ms`；捕获异常转成 `ok=False` 而不是抛给回路；把大结果写 blobs 并只留 `blob_refs`。

### 各工具（都在 `tools/`）
| 文件 | 动作 | 实现要点 |
|---|---|---|
| `t_search_official.py` | `t_search_official` | 包 `fintrace_engine.sourcing.cninfo`：按代码+类别查询与下载，产出 `Document`；离线测试只测参数校验与未命中 |
| `t_parse.py` | `t_parse` | `format_probe` → 分派内置 PDF 提取器 / `docx_extract` / `spec_extract` → `list[Span]` |
| `t_extract.py` | `t_extract` | 调 `fintrace_engine.llm.decompose_claims`；无 key 或失败 → 回落规则拆解，`extracted_by.how="rule"` |
| `t_knowledge.py` | `t_knowledge` | 四个查询接口的薄封装 |
| `t_compute.py` | `t_compute` | 用 `Decimal` 复算；**记 `rounding_step="calc_output"`、`rules_version`、`rule_id`、舍入前全精度值** |
| `t_lookup.py` | `t_lookup` | 查 `FactStore`（默认 `only_facts=True`） |
| `t_recall.py` | `t_recall` | 按关键词/页范围从 `EvidenceLog` 召回 Span 原文（返回原文，不是摘要） |
| `t_review.py` | `t_review` | 论证审查；默认 `enabled=False`，未启用时明确失败 |
| `t_spec_extract.py` | `t_spec_extract` | 先 `validate_spec` 再 `run_spec`；校验失败不许执行 |
| `t_search_web.py` / `t_fetch_web.py` | 同上 | **预留接口**：`reserved=True`，未配置时 `ok=False, error="not_configured"`，绝不发起网络请求 |
| `t_author_tool.py` | `t_author_tool` | **不启用**：`enabled=False`，调用即返回 `disabled`，注释指向 v2.2 §5.4 的 OS 级隔离前置条件 |

**模块 G 必测**：未注册工具被拒；`t_author_tool` 与 web 工具在未启用时明确失败（且不触网）；
`t_compute` 的舍入记录齐全；`t_spec_extract` 拒绝非法规格。

---

## 8. `agent/` —— 回路（主人：模块 H）

### `agent/state.py`
```python
class StateStore:
    def __init__(self, run_id: str, create: bool = True)
    def save(self, state: TaskState) -> None          # 原子写
    def load(self) -> TaskState
    def digest(self, state: TaskState) -> str         # 用 contracts.digest
```

### `agent/bounds.py`（硬边界 + 打转守门）
```python
@dataclass
class BoundVerdict:
    decision: str        # contracts.BoundDecision 值
    code: str            # "ok" / "max_tool_calls" / "token_budget" / "deadline" / "cancelled"
                         # / "tool_permission" / "precondition" / 五种 GuardCode
    reason: str

def may_continue(state: TaskState, *, action: str = "", now: float | None = None) -> BoundVerdict
def guard_checks(steps: list[dict]) -> list[BoundVerdict]     # 纯函数，认五种打转
```
打转五种（v2.2 §4.1）：`exact_failure`、`same_tool_failure`、`idempotent_no_progress`、
`identical_call_streak`（连续 3 次）、`identical_cycle`（周期 2~4，历史窗口 64）。
**一次成功的写入/执行要清零失败签名**（有进展就不算打转）。`may_continue` 只做判定，不抛异常。

### `agent/model.py`
```python
class NotConfigured(RuntimeError): ...
class ModelClient:
    def __init__(self, *, base_url: str = "", model: str = "", api_key: str = "", timeout: int = 60)
    @property
    def configured(self) -> bool
    def decide(self, context: dict) -> tuple[str, dict]      # (原文响应, usage)
    def explain(self, payload: dict) -> str
def parse_decision(text: str) -> dict      # 严格 JSON；非法抛 DecisionError(message)
```
Key 读取顺序：环境变量 `FINTRACE_LLM_API_KEY` / `DEEPSEEK_API_KEY` / `OPENAI_API_KEY` →
`.secrets/llm.json`。**任何情况下不得把 key 写进代码、日志、异常文本**；日志里只准出现掩码。

### `agent/planner.py`
```python
def build_context(state, *, evidence, kb, fact_store, as_of_mode: str) -> dict
def decide(state, client, *, context: dict, prompt_version: str = "p1") -> Decision
```
上下文六块（v2.2 §4.4）：固定头、待解问题、按需取回的原文片段、按需查询的规则、已核验事实、
上一轮收据。**不送**年报全文/历史工具输出/已 close 主张细节。
`as_of` 过滤：`historical_replay` 下 `first_published_at > cutoff` 的线索**不进上下文**，
并把本轮上下文里线索首曝时间的区间写进 `Decision.context_leads_span`。

### `agent/tool_guard.py`
```python
def before_call(name: str, args: dict, state: TaskState, *, kb, facts) -> tuple[bool, str]
def after_call(receipt: ToolReceipt, state: TaskState) -> str   # 返回 ProgressKind 值
```
前置条件（按工具写死）：`t_compute` 需要相关证据已登记；`t_knowledge` 需要查询词非空；
`t_recall` 需要关键词或页范围；`t_extract` 需要目标材料已解析出 Span。
`after_call` 判定进展四类 + `NONE`。

### `agent/loop.py`
```python
def run_loop(*, case, client=None, mode: str = "agent", max_steps: int | None = None,
             run_id: str = "", kb=None, facts=None, tools_ctx=None) -> LoopResult
```
```python
@dataclass
class LoopResult:
    state: TaskState
    findings: list[Finding]
    gate: GateResult | None
    decisions: list[Decision]
    exit: str          # "stop" / "bounds" / "budget_exhausted" / "fixed"
```
流程严格按 v2.2 §4.3：`bounds.may_continue` → `build_context` → `decide` → `tool_guard.before_call`
→ `execute` → W 门 → 原子写 state + 追加 `decisions.jsonl` → 回环。
出口只有三个：硬边界 block、模型 stop 且 V 认可、V 要求补查但额度已尽。
非法决策连续 3 次 → 回落 `fixed`（调 `cases.fixed_runner`）。`mode="fixed"` 时直接走 fixed 链路。
**没有第四个出口**：模型说完了不等于完。

**模块 H 必测**：打转五种各一例；预算耗尽 → `BLOCK`；工具权限之外的动作 → `BLOCK`；
连续 3 次非法决策 → 回落 fixed；用**假模型**（不发网络请求）跑通两轮回路并落盘 `state.json`
与 `decisions.jsonl`；`state.json` 原子写（写一半被杀不会留坏文件——用 try/finally 模拟）。

---

## 9. `cases/` —— 案件适配与评测（主人：模块 I）

### `cases/adapter.py`
```python
@dataclass
class CaseInput:
    case_id: str; company: str; stock_code: str; period: str
    prediction_cutoff_date: str; draft_path: str; expected_path: str
    documents: list[dict]; raw: dict

def load_case(path: str | Path) -> CaseInput
def read_draft(case: CaseInput, root: Path) -> str
def draft_to_claims(draft_text: str, *, case: CaseInput) -> list[Claim]   # 规则拆解，离线
def documents_of(case: CaseInput, root: Path) -> list[Document]
```
`documents_of` 必须把 case 里的相对路径解析成绝对路径并算 `content_hash`；
`prediction_cutoff_date` 进 `Document.available_before_cutoff` 的判定由 W2 做，这里只填原始值。

### `cases/fixed_runner.py`
```python
def run_fixed(case: CaseInput, *, root: Path, use_llm: bool = False) -> FixedResult
```
调 `fintrace_engine.draft_review.run_draft_case(case.raw, use_llm=use_llm)`，
把结果翻成 `list[Finding]`（`verdict.label` → 我们的 `Verdict`；`evidence` 保留
`file_name/page/value/quote`；`basis` 进 `Finding.basis`），并保留原始 `res` 供评测复用。

### `cases/eval_agent.py`（六项新指标 + 基线复用）
```python
def metric_asr(runs: list[dict]) -> dict          # 自主调查成功率
def metric_err(runs: list[dict], gold: dict) -> dict
def metric_itr(runs: list[dict]) -> dict          # 只算「被拒」
def metric_npr(runs: list[dict]) -> dict          # 无进展率
def metric_sr(runs: list[dict]) -> dict           # 主张解决率
def metric_filr(runs: list[dict]) -> dict         # 未来信息泄漏率
def run_baseline(root: Path) -> dict              # 复用 tools/run_draft_eval 的口径
def render_report(metrics: dict) -> str
```
每项返回 `{"value": float|None, "num": int, "den": int, "note": str, "source": "..."}`。
**纯函数**：输入是已落盘的 run 产物（`state.json` / `decisions.jsonl` / `findings.json`），
不自己跑回路。分母为 0 时 `value=None` 并写明原因（不许造假数字）。

**模块 I 必测**：六个指标函数各构造一例（含分母为 0 的边界）；`load_case` 读两个真实 case；
`draft_to_claims` 在两个真实草稿上拆出非空主张且带句子 id。

---

## 10. `reporting/` + `cli.py`（主人：模块 J）

### `reporting/report.py`
```python
def findings_payload(findings: list[Finding], state: TaskState, *, gate: GateResult | None = None) -> dict
def render_markdown(payload: dict) -> str
def write_outputs(run_dir: Path, payload: dict) -> dict   # findings.json / report.md
```
报告必须包含：逐条 Finding（断言/判定/依据/修正建议/引用页）、**未解决清单**（V6）、
一手证据与二手线索分开呈现、免责声明。措辞过 `fintrace_engine.verification.wording_guard`。

### `cli.py`
```python
def main(argv: list[str] | None = None) -> int
```
子命令：`fixed <case_id>`（固定链路基线）、`agent <case_id>`（自主回路）、`eval`（跑评测与六项指标）、
`knowledge build|stats`、`gate <run_id>`、`doctor`（环境自检：解释器、pymupdf、key 是否配置、数据库状态）。

**模块 J 必测**：`--help` 退出码 0；`doctor` 不触网且不打印任何 key 明文；
`render_markdown` 在只有残余项、没有 Finding 时也不崩；措辞守门人拦截定性措辞。

---

## 11. 集成验收（主代理做，不由模块负责人做）

```bash
cd G:/项目/fintrace-agent
.venv/Scripts/python.exe tools/run_draft_eval.py        # 必须仍是 26/26、19/19、退出码 0
.venv/Scripts/python.exe tools/check_guardrails.py      # 六节反例全过
.venv/Scripts/python.exe -m pytest tests -q             # 全绿
.venv/Scripts/python.exe -m fintrace_agent.cli fixed QM-DRAFT-2023-001
.venv/Scripts/python.exe -m fintrace_agent.cli agent QM-DRAFT-2023-001   # 无 key 时也要能跑（回落规则）
```
任何一项掉了就回滚该模块，不许「先合再说」。
