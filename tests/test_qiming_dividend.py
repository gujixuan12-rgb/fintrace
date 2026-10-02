import copy
import json
from pathlib import Path

import pytest

from fintrace.calculators.consistency import dividend_reconciliation

ROOT = Path(__file__).resolve().parents[1]
CASE = json.loads((ROOT / "data/demo/qiming_2023_dividend.json").read_text(encoding="utf-8"))


def test_original_report_dividend_reconciliation():
    from tools.run_qiming_demo import build

    result = build(CASE)
    calc = result["detection"]["calculation"]
    assert result["detection"]["status"] == "REVIEW_REQUIRED"
    assert calc["expected_yuan"] == "4085484.55"
    assert calc["disclosed_yuan"] == "408548.46"
    assert calc["difference_yuan"] == "3676936.09"
    assert calc["other_page_matches_after_rounding"] is True
    assert result["ex_post_evaluation"]["matches_recalculation"] is True


def test_future_original_source_is_rejected():
    from tools.run_qiming_demo import build

    case = copy.deepcopy(CASE)
    case["pre_cutoff_source"]["publication_date"] = "2024-04-23"
    with pytest.raises(ValueError, match="晚于"):
        build(case)


def test_bad_numbers_do_not_look_like_valid_results():
    with pytest.raises(ValueError):
        dividend_reconciliation("NaN", "0.10", "408548.46", "408.55")
