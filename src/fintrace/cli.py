"""fintrace 命令行入口。

    python -m fintrace.cli demo                       # 用内置 mock 跑一遍
    python -m fintrace.cli run data/mock/mock_original_inputs.json --out out/report.json
    python -m fintrace.cli guard "公司虚增收入 2 亿元"  # 单独试措辞守门人

最小链路（真实 PDF）：

    python -m fintrace.cli dividend 年报.pdf --cutoff 2024-04-01 \
        --publication-date 2024-03-29 --company 启明信息 --notice 更正公告.pdf --out out/qiming

大模型（可选，配置见 .secrets/llm.json）：

    python -m fintrace.cli llm                    # 测连通性，真调一次接口
    python -m fintrace.cli dividend 年报.pdf --cutoff 2024-04-01 \
        --publication-date 2024-03-29 --llm       # 跑完再让模型讲成人话

本机核验台（浏览器里手动跑）：

    python -m fintrace.cli ui --port 8911        # 打开 http://127.0.0.1:8911
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import dividend_case, draft_review, llm, orchestrator
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


def _print_dividend_case(res) -> None:
    print(f"运行日志编号 : {res.run_log_id}")
    print(f"被核查材料   : {res.annual_file}（{res.page_count} 页）")
    print(f"预警截止日   : {res.cutoff}")
    hits = "、".join(f"{k} 第 {v} 页" for k, v in res.pages_hit.items() if v)
    print(f"页码定位     : {hits or '无'}")
    base = res.meta.get("base")
    per10 = res.meta.get("per10")
    expected = res.meta.get("expected")
    if expected is not None:
        print(f"复算         : {base:,} 股 × {per10} 元 ÷ 10 = {expected:,} 元")
    print(f"结论         : {res.summary_line()}")
    for f in res.findings:
        print(f"  [{f.severity}] {f.code}　{f.title}")
        print(f"      {f.detail}")
        for e in f.evidence:
            page = f"第 {e.page} 页" if e.page else "页码未取到"
            val = f"　值 = {e.value}" if e.value is not None else ""
            print(f"      - {e.file} {page}：{e.line}{val}")
    if res.notice.get("hit"):
        print(f"事后比对     : 复算值 {res.notice['value']} 出现在 {res.notice['file']} 第 {res.notice['page']} 页")
    elif res.notice.get("checked"):
        print(f"事后比对     : 未在公告中字面命中复算值（{res.notice.get('reason', '')}）")
    if res.saved:
        print(f"已写出       : {res.saved['report']}")
        print(f"已写出       : {res.saved['payload']}")


def _print_llm_explanation(res) -> None:
    """把确定性结果交给模型讲成人话。模型不可用时只提示，不影响核查结论。"""
    cfg = llm.load_config()
    view = llm.public_config(cfg)
    d = res.to_dict()
    print()
    print("-" * 62)
    print(f"AI 解释　模型 {view['model']}　key {view['key_hint'] or '未配置'}")
    print("-" * 62)
    try:
        text = llm.explain(
            annual_file=res.annual_file,
            meta={"复算过程": d["recalculation"], "页码定位": d["pages_hit"]},
            findings=d["findings"],
            report_markdown=res.markdown,
            cfg=cfg,
        )
    except llm.LLMError as e:
        print(f"大模型暂时不可用：{e}")
        return
    print(text)




def _print_draft_review(res) -> None:
    s = res.summary()
    print(f"运行日志编号 : {res.run_log_id}")
    print(f"案例         : {res.case_id}　主体 {res.company}")
    print(f"预测截止日   : {res.cutoff}")
    print(f"主张合计     : {len(res.claims)} 条　支持 {s['SUPPORTED']} / 矛盾 {s['CONTRADICTED']} / "
          f"证据不足 {s['INSUFFICIENT']}")
    for e in res.excluded:
        print(f"  已排除材料 : {e['file_name']}（{e['publication_date']}）")
    print()
    for c in res.claims:
        v = c["verdict"]
        et = f"　{v['error_type']}" if v["error_type"] else ""
        print(f"[{v['label']}{et}] {c['claim_id']}　{c['claim_text'][:60]}")
        print(f"    依据：{v['basis']}")
        if v.get("suggested_fix"):
            print(f"    建议：{v['suggested_fix']}")
        if v.get("missing_input"):
            print(f"    缺失输入：{v['missing_input']}")
        for ev in v.get("evidence", [])[:2]:
            page = f"第 {ev['page']} 页" if ev.get("page") else "页码未取到"
            print(f"    证据：{ev['file_name']} {page}：{ev['quote'][:70]}")
    if res.warnings:
        print()
        print(f"提示 {len(res.warnings)} 条")
        for w in res.warnings[:8]:
            print(f"  - {w}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fintrace", description="财报/研报可追溯风险预警")
    sub = ap.add_subparsers(dest="cmd")

    p_demo = sub.add_parser("demo", help="用内置 mock 数据跑一遍完整流程")
    p_demo.add_argument("--out", default=None)

    p_run = sub.add_parser("run", help="用指定输入 JSON 跑")
    p_run.add_argument("inputs")
    p_run.add_argument("--out", default=None)

    p_div = sub.add_parser("dividend", help="最小链路：原始年报 PDF → 页码定位 → 复算 → 报告")
    p_div.add_argument("pdf", help="被核查的年报 PDF")
    p_div.add_argument("--cutoff", required=True, help="预警截止日 YYYY-MM-DD")
    p_div.add_argument("--publication-date", required=True, help="该年报的发布日期 YYYY-MM-DD")
    p_div.add_argument("--notice", default=None, help="官方更正公告 PDF（可选，仅事后检验）")
    p_div.add_argument("--company", default="", help="主体名称")
    p_div.add_argument("--case-id", default="", help="案例编号，缺省取文件名")
    p_div.add_argument("--out", default=None, help="输出目录（写 payload.json 与 报告.md）")
    p_div.add_argument("--llm", action="store_true", help="跑完再调大模型，把结果讲成人话")

    p_llm = sub.add_parser("llm", help="测大模型连通性（真调一次接口）")
    p_llm.add_argument("--model", default=None, help="临时覆盖模型名")

    p_ui = sub.add_parser("ui", help="启动本机核验台（在浏览器里手动跑最小链路）")
    p_ui.add_argument("--port", type=int, default=8911, help="端口，默认 8911")
    p_ui.add_argument("--host", default="127.0.0.1", help="监听地址（默认只监听本机）")
    p_ui.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")

    p_guard = sub.add_parser("guard", help="试措辞守门人")
    p_guard.add_argument("text")
    p_guard.add_argument("--mode", default="pre_hoc", choices=["pre_hoc", "post_hoc"])

    p_draft = sub.add_parser("draft", help="研报草稿核查闭环：草稿 → 拆主张 → 定位 → 复算 → 判定 → 报告")
    p_draft.add_argument("case", help="案例清单 JSON（材料 + 截止日 + 草稿路径）")
    p_draft.add_argument("--out", default=None, help="输出目录（写 报告.md 与 payload.json）")
    p_draft.add_argument("--no-llm", action="store_true", help="不用大模型，走确定性规则抽取")
    p_draft.add_argument("--evaluate", action="store_true", help="与真值标注对照，输出案例级指标")
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

    if a.cmd == "dividend":
        try:
            res = dividend_case.run_pdf_case(
                a.pdf,
                cutoff_date=a.cutoff,
                publication_date=a.publication_date,
                company=a.company,
                case_id=a.case_id,
                notice_path=a.notice,
                outdir=a.out,
            )
        except FileNotFoundError as e:
            print(f"读不到材料：{e}", file=sys.stderr)
            return 1
        except ImportError as e:
            print(str(e), file=sys.stderr)
            return 1
        except ValueError as e:
            print(f"拒绝：{e}", file=sys.stderr)
            return 1
        _print_dividend_case(res)
        if a.llm:
            _print_llm_explanation(res)
        return 0

    if a.cmd == "llm":
        cfg = llm.load_config()
        if a.model:
            cfg["model"] = a.model
        view = llm.public_config(cfg)
        print(f"base_url : {view['base_url']}")
        print(f"模型     : {view['model']}")
        print(f"key      : {view['key_hint'] or '未配置'}（来源：{view['key_from'] or '—'}）")
        if not view["has_key"]:
            print("\n没有配 key：把 key 放进 .secrets/llm.json，或设环境变量 DEEPSEEK_API_KEY。")
            return 1
        try:
            text = llm.chat([{"role": "user", "content": "只回复两个字：可用"}], cfg=cfg)
        except llm.LLMError as e:
            print(f"\n连通失败：{e}")
            return 1
        print(f"\n连通正常，模型回复：{text}")
        return 0

    if a.cmd == "ui":
        from . import webapp

        webapp.serve(host=a.host, port=a.port, open_browser=not a.no_browser)
        return 0

    if a.cmd == "guard":
        try:
            wg.assert_clean([a.text], mode=a.mode, has_confirmation=a.confirmed)
        except wg.WordingViolation as e:
            print(f"拒绝：{e}")
            return 1
        print("通过：未发现定性断言")
        return 0

    if a.cmd == "draft":
        payload = json.loads(Path(a.case).read_text(encoding="utf-8"))
        res = draft_review.run_draft_case(payload, use_llm=not a.no_llm)
        _print_draft_review(res)
        if a.out:
            out = Path(a.out)
            out.mkdir(parents=True, exist_ok=True)
            (out / "报告.md").write_text(res.markdown, encoding="utf-8")
            (out / "payload.json").write_text(
                json.dumps(res.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"\n已写出：{out / '报告.md'}")
            print(f"已写出：{out / 'payload.json'}")
        if a.evaluate:
            exp = payload.get("expected")
            if exp:
                m = draft_review.evaluate(res, ROOT / exp)
                print("\n--- 案例级指标（样本量小，不得外推）---")
                print(json.dumps({k: v for k, v in m.items() if k != "rows"},
                                 ensure_ascii=False, indent=2))
                for r in m["rows"]:
                    flag = "一致" if r["match"] else "差异"
                    print(f"  {flag} {r['claim_id']}　期望 {r['expected']} / 实得 {r['got']}"
                          f"　错误类型 期望 {r['expected_error_type']} / 实得 {r['got_error_type']}")
        return 0

    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
