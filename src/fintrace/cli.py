"""fintrace 命令行入口。

    python -m fintrace.cli demo                       # 用内置 mock 跑一遍
    python -m fintrace.cli run data/mock/mock_original_inputs.json --out out/report.json
    python -m fintrace.cli guard "公司虚增收入 2 亿元"  # 单独试措辞守门人
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import orchestrator
from .verification import wording_guard as wg

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MOCK = ROOT / "data" / "mock" / "mock_original_inputs.json"


def _print_report(rep) -> None:
    print(f"运行日志编号 : {rep.run_log_id}")
    print(f"主体         : {rep.company}")
    print(f"预警截止日   : {rep.prediction_cutoff_date}    模式: {rep.task_mode}")
    print(f"范围         : {rep.scope_note}")
    print()
    print(f"风险信号 {len(rep.signals)} 条")
    for i, s in enumerate(rep.signals, 1):
        print(f"  {i}. [{s['severity']}] {s['title']}  →  标签：{s['label_cn']}")
        print(f"     依据：{s['basis']}")
        for c in s["suggested_checks"]:
            print(f"     - 建议核查：{c}")
        for e in s["evidence"]:
            print(f"     - 证据：{e['file_name']} 第 {e['page']} 页（发布 {e['publication_date']}）")
    if rep.excluded:
        print()
        print(f"被时间过滤排除的材料 {len(rep.excluded)} 份")
        for e in rep.excluded:
            print(f"  - {e['file_name']}（{e['publication_date']}）：{'；'.join(e['reasons'])}")
    if rep.unable_to_judge:
        print()
        print(f"无法判断 {len(rep.unable_to_judge)} 项")
        for u in rep.unable_to_judge:
            print(f"  - {u['claim_id']}：{u['reason']}（置信度 {u['confidence']}）")
    if rep.warnings:
        print()
        print(f"提示 {len(rep.warnings)} 条")
        for w in rep.warnings:
            print(f"  - {w}")
    print()
    print(rep.disclaimer)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fintrace", description="财报/研报可追溯风险预警")
    sub = ap.add_subparsers(dest="cmd")

    p_demo = sub.add_parser("demo", help="用内置 mock 数据跑一遍完整流程")
    p_demo.add_argument("--out", default=None)

    p_run = sub.add_parser("run", help="用指定输入 JSON 跑")
    p_run.add_argument("inputs")
    p_run.add_argument("--out", default=None)

    p_guard = sub.add_parser("guard", help="试措辞守门人")
    p_guard.add_argument("text")
    p_guard.add_argument("--mode", default="pre_hoc", choices=["pre_hoc", "post_hoc"])
    p_guard.add_argument("--confirmed", action="store_true", help="存在监管/公司确认文件")

    a = ap.parse_args(argv)

    if a.cmd in (None, "demo", "run"):
        path = DEFAULT_MOCK if a.cmd in (None, "demo") else Path(a.inputs)
        res = orchestrator.run_files(path)
        _print_report(res.report)
        if a.out:
            out = Path(a.out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(res.report.to_json(), encoding="utf-8")
            rec_path = out.with_name(out.stem + ".records.json")
            rec_path.write_text(
                json.dumps([r.to_dict() for r in res.records], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print()
            print(f"已写出：{out}")
            print(f"已写出：{rec_path}")
        return 0

    if a.cmd == "guard":
        try:
            wg.assert_clean([a.text], mode=a.mode, has_confirmation=a.confirmed)
        except wg.WordingViolation as e:
            print(f"拒绝：{e}")
            return 1
        print("通过：未发现定性断言")
        return 0

    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
