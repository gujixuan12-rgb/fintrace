"""从人工复核的两页摘录生成可复算JSON，事后公告仅用于验证。"""
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from fintrace.calculators.consistency import dividend_reconciliation


def build(case: dict) -> dict:
    cutoff = date.fromisoformat(case["prediction_cutoff_date"])
    before = case["pre_cutoff_source"]
    after = case["post_cutoff_validation"]
    if before["source_type"] != "original_annual_report":
        raise ValueError("检测输入必须来自原始年报")
    if date.fromisoformat(before["publication_date"]) > cutoff:
        raise ValueError("原始年报晚于检测截止日")
    if after["allowed_for_detection"] is not False:
        raise ValueError("事后验证材料不可进入检测输入")
    if date.fromisoformat(after["publication_date"]) <= cutoff:
        raise ValueError("事后验证材料日期必须晚于截止日")
    observations = case["observations"]
    calculations = dividend_reconciliation(
        observations["shares"]["value"],
        observations["dividend_per_ten_shares"]["value"],
        observations["disclosed_dividend"]["value"],
        observations["other_page_total"]["value"],
    )
    return {
        "case_id": case["case_id"],
        "company": case["company"],
        "prediction_cutoff_date": case["prediction_cutoff_date"],
        "input_mode": case["input_mode"],
        "detection": {
            "status": "REVIEW_REQUIRED" if not calculations["disclosure_matches"] else "CONSISTENT",
            "claim": "第51页现金分红金额与股本、派息额及第223页总额不一致",
            "evidence": [
                {"printed_page": observations[key]["printed_page"],
                 "metric": observations[key]["description"], "value": observations[key]["value"],
                 "unit": observations[key]["unit"], "file": before["file_name"]}
                for key in ("shares", "dividend_per_ten_shares", "disclosed_dividend", "other_page_total")
            ],
            "calculation": calculations,
            "suggestion": "复核第51页现金分红金额，并与第223页利润分配总额统一披露。",
            "boundary": "仅识别披露勾稽矛盾，不判断成因或责任。",
        },
        "ex_post_evaluation": {
            "source_url": after["url"],
            "corrected_yuan": after["corrected_dividend_yuan"],
            "matches_recalculation": after["corrected_dividend_yuan"] == calculations["expected_yuan"],
        },
    }


if __name__ == "__main__":
    data = json.loads((ROOT / "data/demo/qiming_2023_dividend.json").read_text(encoding="utf-8"))
    report = build(data)
    destination = ROOT / "out/qiming_2023_dividend_report.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(destination)
