"""命令行入口。

    python -m fintrace_agent.cli list                     列出案件
    python -m fintrace_agent.cli fixed QM-DRAFT-2023-001  固定链路（基线）
    python -m fintrace_agent.cli agent QM-DRAFT-2023-001  自主回路（无 key 时回落基线）
    python -m fintrace_agent.cli eval                     跑基线口径 + 六项新指标
    python -m fintrace_agent.cli gate <run_id>            只看某次运行的发布决定
    python -m fintrace_agent.cli knowledge stats|build    知识库
    python -m fintrace_agent.cli doctor                   环境自检（不触网、不打印任何 key）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from fintrace_agent import __version__, paths as P


def _print(msg: str = "") -> None:
    print(msg)


def cmd_list(_args) -> int:
    from fintrace_agent.cases.adapter import find_case, list_cases

    for stem in list_cases():
        try:
            c = find_case(stem)
            _print(f"  {c.case_id:<22} {c.company} {c.period}　截止日 {c.prediction_cutoff_date}"
                   f"　材料 {len(c.documents)} 份")
        except Exception as e:                                    # noqa: BLE001
            _print(f"  {stem:<22}（读不出：{type(e).__name__}: {e}）")
    return 0


def cmd_fixed(args) -> int:
    from fintrace_agent.pipeline import run_fixed_case

    out = run_fixed_case(args.case_id, use_llm=args.use_llm)
    _report_outcome(out)
    return 0


def cmd_agent(args) -> int:
    from fintrace_agent.agent.model import ModelClient
    from fintrace_agent.pipeline import run_agent_case

    client = None
    try:
        client = ModelClient()
    except Exception as e:                                        # noqa: BLE001
        _print(f"（模型客户端初始化失败，将回落固定链路：{type(e).__name__}）")
    out = run_agent_case(args.case_id, client=client, max_steps=args.max_steps)
    _report_outcome(out)
    return 0


def _report_outcome(out) -> None:
    _print(f"运行编号 {out.run_id}　模式 {out.mode}　出口 {out.exit}")
    _print(f"产物目录 {out.run_dir}")
    counts = out.counts()
    _print("结论分布：" + ("、".join(f"{k} {v}" for k, v in sorted(counts.items())) or "（无）"))
    gate_counts = dict(getattr(out.gate, "counts", {}) or {})
    if gate_counts:
        _print("发布决定：" + "、".join(f"{k} {v}" for k, v in sorted(gate_counts.items())))
    for n in out.notes:
        _print(f"注：{n}")
    _print(f"报告 {out.outputs.get('report_md', '')}")


def cmd_eval(args) -> int:
    from fintrace_agent.cases.eval_agent import collect_metrics, render_report, run_baseline

    base = run_baseline(use_llm=args.use_llm)
    _print("=== 固定链路基线（复用团队仓库口径，案例级，不得外推）===")
    _print(f"  案件：{ '、'.join(base['cases']) }")
    _print(f"  真值主张 {base['gold_claims']} 条，匹配 {base['matched_claims']} 条"
           f"（覆盖 {base['coverage']}），标签一致率 {base['label_accuracy']}")
    d = base["error_detection"]
    _print(f"  错误识别 TP {d['tp']} / FP {d['fp']} / FN {d['fn']}")
    c = base["content"]
    _print(f"  内容断言：类型 {c['type_ok']}/{c['type_ok'] + c['type_bad']}　"
           f"修正值 {c['fix_ok']}/{c['fix_ok'] + c['fix_bad']}　"
           f"落页 {c['page_ok']}/{c['page_ok'] + c['page_bad']}　"
           f"原件回查 {c['pdf_ok']}/{c['pdf_ok'] + c['pdf_bad']}")
    _print(f"  基线退出码 {base['exit_code']}（口径：{base['verdict']}）")
    _print()
    _print("=== Agent 模式新指标 ===")
    m = collect_metrics()
    _print(render_report(m))
    return int(base["exit_code"])


def cmd_gate(args) -> int:
    from fintrace_agent.store.runs import run_dir

    d = run_dir(args.run_id)
    fj = d / "findings.json"
    if not fj.is_file():
        _print(f"找不到 {fj}")
        return 2
    payload = json.loads(fj.read_text(encoding="utf-8"))
    _print(f"运行 {payload.get('run_id', '')}　模式 {payload.get('mode', '')}")
    _print("发布决定：" + json.dumps(payload.get("gate_counts") or {}, ensure_ascii=False))
    for f in payload.get("findings") or []:
        _print(f"  {f.get('finding_id', ''):<12} {f.get('verdict', ''):<14}"
               f" {f.get('publish', ''):<14} {'、'.join(f.get('block_reasons') or [])}")
    res = payload.get("residuals") or []
    _print(f"残余项 {len(res)} 条")
    return 0


def cmd_knowledge(args) -> int:
    from fintrace_agent.knowledge.build import build_db
    from fintrace_agent.knowledge.loader import KnowledgeBase

    if args.action == "build":
        rows = build_db(P.KNOWLEDGE_DATA, P.KNOWLEDGE_DB)
        _print("知识库已重建：" + json.dumps(rows, ensure_ascii=False))
        return 0
    kb = KnowledgeBase(P.KNOWLEDGE_DB)
    metrics = kb.all_metrics()
    _print(f"指标 {len(metrics)} 个；库文件 {P.KNOWLEDGE_DB}（{P.KNOWLEDGE_DB.stat().st_size} 字节）")
    return 0


def cmd_doctor(_args) -> int:
    """环境自检：不触网、不打印任何 key 明文。"""
    _print(f"版本 {__version__}　Python {sys.version.split()[0]}")
    _print(f"项目根 {P.ROOT}")
    try:
        import pymupdf
        _print(f"pymupdf {pymupdf.__doc__ or 'ok'}")
    except ImportError:
        _print("pymupdf 未安装：读真实 PDF 会降级（离线流程不受影响）")
    _print(f"知识库 {'存在' if P.KNOWLEDGE_DB.is_file() else '缺失'}"
           f"（{P.KNOWLEDGE_DB.stat().st_size if P.KNOWLEDGE_DB.is_file() else 0} 字节）")
    _print(f"事实库 {'存在' if P.FACTS_DB.is_file() else '还没建'}")
    run_dirs = [p for p in P.runs_dir().iterdir()] if P.runs_dir().is_dir() else []
    _print(f"运行记录 {len(run_dirs)} 个（{P.runs_dir()}）")

    env_hit = [k for k in ("FINTRACE_LLM_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY")
               if os.environ.get(k)]
    _print(f"模型 key：环境变量 {'命中 ' + '、'.join(env_hit) if env_hit else '未命中'}；"
           f"本机配置 {'有' if P.SECRETS.is_file() else '无'}（明文永不出后端）")
    from fintrace_agent.agent.model import ModelClient

    c = ModelClient()
    _print(f"模型可用：{'是' if c.configured else '否'}（未配置时自动回落固定链路）")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fintrace-agent", description="FinTrace 独立版")
    p.add_argument("--version", action="version", version=f"fintrace-agent {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="列出案件").set_defaults(func=cmd_list)

    f = sub.add_parser("fixed", help="固定链路（基线）")
    f.add_argument("case_id")
    f.add_argument("--use-llm", action="store_true", help="允许调用模型做草稿拆解")
    f.set_defaults(func=cmd_fixed)

    a = sub.add_parser("agent", help="自主回路（无 key 时回落基线）")
    a.add_argument("case_id")
    a.add_argument("--max-steps", type=int, default=None)
    a.set_defaults(func=cmd_agent)

    e = sub.add_parser("eval", help="跑基线口径与六项新指标")
    e.add_argument("--use-llm", action="store_true")
    e.set_defaults(func=cmd_eval)

    g = sub.add_parser("gate", help="看某次运行的发布决定")
    g.add_argument("run_id")
    g.set_defaults(func=cmd_gate)

    k = sub.add_parser("knowledge", help="知识库")
    k.add_argument("action", choices=("stats", "build"))
    k.set_defaults(func=cmd_knowledge)

    sub.add_parser("doctor", help="环境自检").set_defaults(func=cmd_doctor)
    return p


def main(argv: list[str] | None = None) -> int:
    paths_ok = Path(P.ROOT).is_dir()
    if not paths_ok:                                             # pragma: no cover
        print("项目根解析异常，请检查安装方式", file=sys.stderr)
        return 2
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":                                       # pragma: no cover
    raise SystemExit(main())
