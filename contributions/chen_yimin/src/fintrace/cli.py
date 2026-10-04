from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from pathlib import Path
from typing import Any

from .fact_checker import check_claims
from .orchestrator import run_fintrace
from .pipeline import run_case
from .reporting import render_html


def _load(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _save(path: str | Path, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _demo_paths(root: Path) -> dict[str, Path]:
    case_dir = root / "cases" / "meichen_2020"
    return {
        "case": case_dir / "original_inputs.json",
        "claims": case_dir / "report_claims.json",
        "ledger": case_dir / "reference_ledger.json",
        "evaluation": case_dir / "evaluation_map.json",
        "json": root / "outputs" / "demo" / "fintrace_full_result.json",
        "html": root / "outputs" / "demo" / "fintrace_demo_report.html",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fintrace", description="证据可追溯的事前财务风险预警")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="运行完整预警流程")
    run.add_argument("input")
    run.add_argument("output")
    run.add_argument("--claims")
    run.add_argument("--ledger")
    run.add_argument("--evaluation-map")
    run.add_argument("--html")
    run.add_argument("--use-llm", action="store_true")

    check = sub.add_parser("check", help="只运行研报事实核验")
    check.add_argument("claims")
    check.add_argument("ledger")
    check.add_argument("case")
    check.add_argument("output")

    demo = sub.add_parser("demo", help="运行内置美晨生态演示")
    demo.add_argument("--open", action="store_true", dest="open_browser")
    demo.add_argument("--use-llm", action="store_true")
    return parser


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] not in {"run", "check", "demo"}:
        _save(sys.argv[2], run_case(_load(sys.argv[1])))
        return 0

    args = build_parser().parse_args()
    if args.command == "check":
        case_result = run_case(_load(args.case))
        _save(args.output, check_claims(_load(args.claims), _load(args.ledger), case_result["features"]))
        return 0

    if args.command == "run":
        result = run_fintrace(
            _load(args.input),
            claims=_load(args.claims) if args.claims else None,
            ledger=_load(args.ledger) if args.ledger else None,
            evaluation_map=_load(args.evaluation_map) if args.evaluation_map else None,
            use_llm=args.use_llm,
        )
        _save(args.output, result)
        if args.html:
            render_html(result, args.html)
        return 0

    root = Path(__file__).resolve().parents[2]
    paths = _demo_paths(root)
    result = run_fintrace(
        _load(paths["case"]),
        claims=_load(paths["claims"]),
        ledger=_load(paths["ledger"]),
        evaluation_map=_load(paths["evaluation"]),
        use_llm=args.use_llm,
    )
    _save(paths["json"], result)
    render_html(result, paths["html"])
    print(f"JSON: {paths['json']}")
    print(f"HTML: {paths['html']}")
    if args.open_browser:
        webbrowser.open(paths["html"].resolve().as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
