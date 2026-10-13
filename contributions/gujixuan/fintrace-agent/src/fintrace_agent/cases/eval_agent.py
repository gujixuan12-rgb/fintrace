"""六项新的 Agent 指标 + 固定链路基线复用（v2.2 §9.2）。

两条纪律写在代码里，不靠记性：
1. 指标函数是**纯函数**：输入是已落盘的 run 产物，不自己跑回路。
2. 分母为 0 时 `value=None` 并写明原因 —— 不许拿 0 或 1 顶替，那是造数字。

每个指标返回 {"value", "num", "den", "note", "source"}，其中 `source` 指明数据来自哪个文件，
方便别人复核。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from fintrace_agent import paths as P
from fintrace_agent.contracts import ProgressKind

#: 指标口径版本 —— 口径改了要改这里，历史读数才知道按哪版算的
METRICS_VERSION = "v2.2-1"


# ---------------------------------------------------------------------------
# 读 run 产物
# ---------------------------------------------------------------------------


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue                     # 半截行（崩溃残留）跳过，不因一行坏掉整份读数
    return out


def load_run(run_dir: str | Path) -> dict:
    """把一个运行目录读成统一的 run 字典；缺文件不算错（读到的就是读到的）。"""
    d = Path(run_dir)
    state = {}
    sp = d / "state.json"
    if sp.is_file():
        state = json.loads(sp.read_text(encoding="utf-8"))
    findings = []
    fp = d / "findings.json"
    if fp.is_file():
        raw = json.loads(fp.read_text(encoding="utf-8"))
        findings = raw.get("findings", raw) if isinstance(raw, dict) else raw

    stop = (state.get("stop") or {}) if isinstance(state, dict) else {}
    mode = str(state.get("mode") or "agent")
    if mode == "fixed":
        exit_reason = "fixed"
    elif stop.get("requested_by_model"):
        exit_reason = "stop"
    else:
        exit_reason = "unknown"

    return {
        "run_id": str(state.get("run_id") or d.name),
        "mode": mode,
        "state": state,
        "decisions": _read_jsonl(d / "decisions.jsonl"),
        "receipts": _read_jsonl(d / "receipts.jsonl"),
        "rejections": _read_jsonl(d / "rejections.jsonl"),
        "findings": list(findings or []),
        "cutoff": str(((state.get("bounds") or {}) if isinstance(state, dict) else {}).get("cutoff_date") or state.get("as_of") or ""),
        "exit": exit_reason,
        "run_dir": str(d),
    }


# ---------------------------------------------------------------------------
# 六项指标
# ---------------------------------------------------------------------------


def _result(value, num, den, note, source) -> dict:
    if den == 0:
        return {"value": None, "num": num, "den": den, "source": source,
                "note": (note + "；分母为 0，不给数字").strip("；")}
    return {"value": round(num / den, 4), "num": num, "den": den,
            "source": source, "note": note}


def metric_asr(runs: list[dict]) -> dict:
    """自主调查成功率：回路在预算内收敛、且没有红线违规的 run 占比。

    口径（先建基线，样本 > 20 前不对外称成功率）：
    分子 = exit == "stop" 且展出的 findings 里没有红线违规记录的 run；
    分母 = 全部 run。红线违规由 `run["red_line_violations"]` 提供（时间截断、二手线索作证据等）。
    """
    good = 0
    for r in runs:
        if r.get("exit") != "stop":
            continue
        if r.get("red_line_violations"):
            continue
        good += 1
    return _result(None, good, len(runs),
                   "收敛且零红线违规的 run 数 / 全部 run（含未收敛与非回路 run，口径偏严）",
                   "state.json + findings.json")


def metric_err(runs: list[dict], gold: dict[str, set[int]]) -> dict:
    """证据召回率（案例级粗口径）：真值标注的应引页里，实际被引到的比例。

    gold: {case_id: {页码, ...}}。分子 = 命中页数，分母 = 该案金标页数。
    单个 run 缺 case_id 时不进统计，并在 note 里说明。
    """
    num = 0
    den = 0
    for r in runs:
        cid = str((r.get("state") or {}).get("goal", {}).get("case_id") or "")
        pages = gold.get(cid)
        if not pages:
            continue
        got = {int(e["page"]) for f in r.get("findings", [])
               for e in (f.get("evidence") or []) if e.get("page")}
        num += len(got & pages)
        den += len(pages)
    return _result(None, num, den,
                   "案例级页集合交集 / 金标页集合（不看同值多页，粗口径，只作趋势）",
                   "data/annotations/evaluation/*.json + findings.json")


def metric_itr(runs: list[dict]) -> dict:
    """无效工具调用率：**被拒**的调用占全部调用的比例。

    只算「被拒」：硬边界拒绝 + schema 非法 + 工具报错。
    「执行成功但没推动调查」不算被拒，那是 NPR（见 metric_npr）。
    """
    bad = tot = 0
    for r in runs:
        for d in r.get("decisions", []):
            tot += 1
            if not d.get("valid", True):
                bad += 1
            elif str(d.get("bound_decision") or "allow") in ("block", "halt"):
                bad += 1
    return _result(None, bad, tot, "被拒调用 / 全部调用", "decisions.jsonl")


def metric_npr(runs: list[dict]) -> dict:
    """无进展率：执行成功但没推动调查的比例。

    进展四类（任一即算进展）：主张状态变化、假设被排除、新增登记证据、证据被否决。
    「证据被否决」也算进展 —— 排除一个错误假设本身就是有效调查。
    进度记录优先取 receipts.jsonl，缺了就从 state.steps 里取。
    """
    none_ct = tot = 0
    for r in runs:
        rows = r.get("receipts") or (r.get("state") or {}).get("steps") or []
        for row in rows:
            prog = str(row.get("progress") or "")
            if not prog:
                continue
            tot += 1
            if prog == ProgressKind.NONE.value:
                none_ct += 1
    return _result(None, none_ct, tot,
                   "无进展调用 / 有进度记录的调用", "receipts.jsonl 或 state.steps")


def metric_sr(runs: list[dict]) -> dict:
    """主张解决率：本轮待核主张里被真正解决的比例，并要求报告带未解决清单。"""
    solved = tot = 0
    for r in runs:
        for c in (r.get("state") or {}).get("claims", []):
            tot += 1
            if c.get("status") in ("verified", "insufficient", "blocked"):
                solved += 1
    return _result(None, solved, tot,
                   "已收敛主张 / 全部待核主张（未解决清单必须随报告输出）",
                   "state.json")


def metric_filr(runs: list[dict]) -> dict:
    """未来信息泄漏率：决策上下文里出现过 cutoff 之后线索的轮次占比。

    historical_replay 模式下这是红线：非零即该次运行作废。
    判据是 decisions.jsonl 里的 context_leads_span（本轮上下文里线索首曝时间的区间），
    而不是「线索有没有被当成证据」—— 影响注意力也是泄漏。
    """
    bad = tot = 0
    for r in runs:
        cutoff = str(r.get("cutoff") or "")
        for d in r.get("decisions", []):
            tot += 1
            span = d.get("context_leads_span") or ["", ""]
            lo, hi = (list(span) + ["", ""])[:2]
            hi = str(hi or lo or "")
            if cutoff and hi and hi > cutoff:
                bad += 1
    return _result(None, bad, tot,
                   "含超截止日线索的决策轮次 / 全部轮次（historical_replay 下红线为 0）",
                   "decisions.jsonl + state.bounds.cutoff_date")


# ---------------------------------------------------------------------------
# 固定链路基线（复用团队仓库口径，不另写一套）
# ---------------------------------------------------------------------------


def _load_eval_module():
    path = P.ROOT / "tools" / "run_draft_eval.py"
    spec = importlib.util.spec_from_file_location("_ft_run_draft_eval", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def run_baseline(*, root: Path | None = None, use_llm: bool = False,
                 quiet: bool = True) -> dict:
    """跑固定链路基线：口径完全复用 tools/run_draft_eval.py，不另立一套。"""
    mod = _load_eval_module()
    cases = sorted((root or P.ROOT).joinpath("data/annotations/cases").glob("*.json"))
    per_case = []
    for p in cases:
        metrics = mod.run_one(p, use_llm=use_llm, save_dir=None)
        per_case.append(metrics)

    keys = ("type_ok", "type_bad", "fix_ok", "fix_bad", "fix_absent",
            "page_ok", "page_bad", "page_absent", "pdf_ok", "pdf_bad", "pdf_unverified")
    agg = {k: sum(m["content"][k] for m in per_case) for k in keys}
    extra_fp = sum(m["extra_outputs"].get("as_fp", 0) for m in per_case)
    extra_review = sum(m["extra_outputs"].get("needs_review", 0) for m in per_case)
    incomplete = [m["case_id"] for m in per_case if not m.get("gold_complete")]
    code, why = mod.verdict_of(agg, {"fp": extra_fp, "review": extra_review,
                                     "incomplete": incomplete})
    gold = sum(m["gold_claims"] for m in per_case)
    matched = sum(m["matched_claims"] for m in per_case)
    hit = sum(1 for m in per_case for r in m["rows"] if r["match"])
    tp = sum(m["error_detection"]["tp"] for m in per_case)
    fp = sum(m["error_detection"]["fp"] for m in per_case)
    fn = sum(m["error_detection"]["fn"] for m in per_case)
    return {
        "cases": [m["case_id"] for m in per_case],
        "gold_claims": gold, "matched_claims": matched, "label_hit": hit,
        "coverage": round(matched / gold, 4) if gold else None,
        "label_accuracy": round(hit / gold, 4) if gold else None,
        "error_detection": {"tp": tp, "fp": fp, "fn": fn},
        "content": agg,
        "exit_code": code,
        "verdict": why,
        "note": "案例级读数，不得外推为泛化准确率",
    }


def collect_metrics(*, root: Path | None = None, runs: list[str] | None = None,
                    gold: dict[str, set[int]] | None = None) -> dict:
    """汇总一份可读的指标表：固定链路基线 + 六项新指标。"""
    root = root or P.ROOT
    run_dirs = [Path(r) for r in (runs or [])]
    if not run_dirs and P.runs_dir().is_dir():
        run_dirs = [p for p in sorted(P.runs_dir().iterdir()) if p.is_dir()]
    loaded_all = [load_run(p) for p in run_dirs]
    # 口径：六项新指标只在 agent 模式的 run 上算。固定链路是基线，混进来会把
    # 分母撑大、把读数压低（ASR 那种「0/5」就是这么来的），两套必须分开报。
    loaded = [r for r in loaded_all if r.get("mode") == "agent"]
    excluded = [r["run_id"] for r in loaded_all if r.get("mode") != "agent"]

    if gold is None:
        gold = {}
        for p in (root / "data/annotations/cases").glob("*.json"):
            case = json.loads(p.read_text(encoding="utf-8"))
            exp_path = case.get("expected")
            if not exp_path:
                continue
            exp = json.loads((root / exp_path).read_text(encoding="utf-8"))
            pages = {int(e["pdf_page"]) for c in exp.get("claims", [])
                     for e in (c.get("evidence") or []) if e.get("pdf_page")}
            gold[str(case.get("case_id"))] = pages

    out = {
        "metrics_version": METRICS_VERSION,
        "runs": [r["run_id"] for r in loaded],
        "excluded_runs": excluded,
        "asr": metric_asr(loaded),
        "err": metric_err(loaded, gold),
        "itr": metric_itr(loaded),
        "npr": metric_npr(loaded),
        "sr": metric_sr(loaded),
        "filr": metric_filr(loaded),
    }
    return out


def render_report(m: dict) -> str:
    """把指标渲染成人能读的一段；没读数就写「还没读数」，不写 0。"""
    lines = [f"指标口径版本 {m.get('metrics_version', '?')}",
             f"参与统计的 agent 运行：{len(m.get('runs') or [])} 个"]
    if m.get("excluded_runs"):
        lines.append(f"  已排除固定链路基线运行 {len(m['excluded_runs'])} 个"
                     f"（{ '、'.join(m['excluded_runs'][:3]) }…），基线口径另报")
    names = {"asr": "自主调查成功率 ASR", "err": "证据召回率 ERR", "itr": "无效工具调用率 ITR",
             "npr": "无进展率 NPR", "sr": "主张解决率 SR", "filr": "未来信息泄漏率 FILR"}
    for key, name in names.items():
        item = m.get(key) or {}
        val = item.get("value")
        shown = "暂无读数" if val is None else f"{val:.4f}"
        lines.append(f"  {name:<22} {shown}  （{item.get('num', '?')}/{item.get('den', '?')}）"
                     f"  来源 {item.get('source', '?')}  {item.get('note', '')}")
    if m.get("rba_suspended"):
        lines.append("  关系绑定准确率 RBA     挂起（等人工标注流程），不计入验收")
    lines.append("纪律：以上是案例级读数，不得外推为泛化能力；固定链路基线与新指标分开报。")
    return "\n".join(lines)
