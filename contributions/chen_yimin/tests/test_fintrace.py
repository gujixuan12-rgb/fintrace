import copy
import json
import sys
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fintrace.calculations import CalculationError, calculate_features, growth_rate, safe_divide
from fintrace.agent import deterministic_explanation
from fintrace.evaluation import evaluate_case
from fintrace.fact_checker import check_claims
from fintrace.normalizers import NormalizationError, normalize_amount, to_decimal
from fintrace.orchestrator import run_fintrace
from fintrace.pipeline import run_case
from fintrace.source_guard import SourceLeakageError, validate_sources
from fintrace.structural_flags import build_structural_flags
from fintrace.thresholds import assess_peer_value


CASE_PATH = ROOT / "cases" / "meichen_2020" / "original_inputs.json"


def load_case():
    return json.loads(CASE_PATH.read_text(encoding="utf-8"))


class NormalizerTests(unittest.TestCase):
    def test_decimal_string_exact(self):
        self.assertEqual(to_decimal("0.1"), Decimal("0.1"))

    def test_decimal_commas(self):
        self.assertEqual(to_decimal("1,234.50"), Decimal("1234.50"))

    def test_boolean_rejected(self):
        with self.assertRaises(NormalizationError):
            to_decimal(True)

    def test_yuan_to_yuan(self):
        self.assertEqual(normalize_amount("12.34", "元"), Decimal("12.34"))

    def test_ten_thousand_to_yuan(self):
        self.assertEqual(normalize_amount("1.25", "万元"), Decimal("12500.00"))

    def test_hundred_million_to_ten_thousand(self):
        self.assertEqual(normalize_amount("2", "亿元", "万元"), Decimal("20000"))

    def test_unsupported_unit(self):
        with self.assertRaises(NormalizationError):
            normalize_amount("1", "%")


class CalculationTests(unittest.TestCase):
    def test_safe_divide(self):
        self.assertEqual(safe_divide(Decimal("1"), Decimal("4")), Decimal("0.25"))

    def test_zero_denominator(self):
        with self.assertRaises(CalculationError):
            safe_divide(Decimal("1"), Decimal("0"))

    def test_growth(self):
        self.assertEqual(growth_rate(Decimal("125"), Decimal("100")), Decimal("0.25"))

    def test_negative_growth(self):
        self.assertEqual(growth_rate(Decimal("80"), Decimal("100")), Decimal("-0.2"))

    def test_case_feature_count(self):
        self.assertEqual(len(calculate_features(load_case()["financials"])), 8)

    def test_revenue_growth(self):
        value = calculate_features(load_case()["financials"])["revenue_growth_2020"]
        self.assertEqual(value.quantize(Decimal("0.000001")), Decimal("0.037671"))

    def test_profit_growth(self):
        value = calculate_features(load_case()["financials"])["attributable_profit_growth_2020"]
        self.assertEqual(value.quantize(Decimal("0.000001")), Decimal("-0.358476"))

    def test_asset_concentration(self):
        value = calculate_features(load_case()["financials"])["receivables_contract_assets_to_total_assets_2020"]
        self.assertEqual(value.quantize(Decimal("0.000001")), Decimal("0.614146"))

    def test_missing_metric(self):
        case = load_case()
        del case["financials"]["2020"]["revenue"]
        with self.assertRaises(CalculationError):
            calculate_features(case["financials"])

    def test_scope_mismatch(self):
        case = load_case()
        case["financials"]["2020"]["operating_cash_flow"]["scope"] = "母公司"
        with self.assertRaises(NormalizationError):
            calculate_features(case["financials"])


class SourceGuardTests(unittest.TestCase):
    def test_missing_date_warning(self):
        warnings = validate_sources(load_case())
        self.assertEqual(len(warnings), 1)

    def test_future_date_blocked(self):
        case = load_case()
        case["sources"][0]["publication_date"] = "2021-05-01"
        with self.assertRaises(SourceLeakageError):
            validate_sources(case)

    def test_ex_post_type_blocked(self):
        case = load_case()
        case["sources"][0]["source_type"] = "correction_announcement"
        with self.assertRaises(SourceLeakageError):
            validate_sources(case)

    def test_explicit_disallow_blocked(self):
        case = load_case()
        case["sources"][0]["allowed_for_model_input"] = False
        with self.assertRaises(SourceLeakageError):
            validate_sources(case)

    def test_unknown_source_blocked(self):
        case = load_case()
        case["financials"]["2020"]["revenue"]["source_id"] = "unknown"
        with self.assertRaises(SourceLeakageError):
            validate_sources(case)

    def test_valid_known_date(self):
        case = load_case()
        case["sources"][0]["publication_date"] = "2021-04-19"
        self.assertEqual(validate_sources(case), [])


class StructuralFlagTests(unittest.TestCase):
    def setUp(self):
        self.features = calculate_features(load_case()["financials"])

    def test_three_signals(self):
        self.assertEqual(len(build_structural_flags(self.features)), 3)

    def test_cash_turn_signal(self):
        ids = {x["signal_id"] for x in build_structural_flags(self.features)}
        self.assertIn("CASH_001", ids)

    def test_divergence_signal(self):
        ids = {x["signal_id"] for x in build_structural_flags(self.features)}
        self.assertIn("DIV_001", ids)

    def test_quality_signal(self):
        ids = {x["signal_id"] for x in build_structural_flags(self.features)}
        self.assertIn("QUALITY_001", ids)

    def test_no_cash_turn_when_both_positive(self):
        features = copy.deepcopy(self.features)
        features["ocf_to_revenue_2020"] = Decimal("0.01")
        ids = {x["signal_id"] for x in build_structural_flags(features)}
        self.assertNotIn("CASH_001", ids)


class PipelineTests(unittest.TestCase):
    def test_end_to_end(self):
        output = run_case(load_case())
        self.assertEqual(output["case_id"], "meichen_2020_pre_event")
        self.assertEqual(len(output["risk_signals"]), 3)

    def test_cautious_role(self):
        output = run_case(load_case())
        self.assertIn("不构成", output["model_role"])

    def test_no_ex_post_conclusion_in_signals(self):
        output = run_case(load_case())
        text = json.dumps(output["risk_signals"], ensure_ascii=False)
        for banned in ["确认舞弊", "确认错报", "财务造假"]:
            self.assertNotIn(banned, text)

    def test_evidence_gap_includes_threshold_limit(self):
        output = run_case(load_case())
        self.assertTrue(any("单一案例" in item for item in output["evidence_gaps"]))

    def test_decimal_results_are_strings(self):
        output = run_case(load_case())
        self.assertTrue(all(isinstance(v, str) for v in output["features"].values()))

    def test_peer_threshold_not_provided_by_default(self):
        output = run_case(load_case())
        self.assertEqual(output["peer_threshold_assessment"]["status"], "NOT_PROVIDED")


class ThresholdTests(unittest.TestCase):
    def test_small_peer_sample_is_not_forced_into_threshold(self):
        result = assess_peer_value("0.60", ["0.10", "0.20", "0.30", "0.40", "0.50", "0.55", "0.70"])
        self.assertEqual(result.status, "INSUFFICIENT_PEER_SAMPLE")
        self.assertIsNone(result.robust_z)

    def test_robust_statistics_with_eligible_sample(self):
        peers = [Decimal(i) / Decimal("100") for i in range(10, 110, 10)]
        result = assess_peer_value(Decimal("1.20"), peers)
        self.assertEqual(result.status, "AVAILABLE")
        self.assertEqual(result.sample_size, 10)
        self.assertIsNotNone(result.robust_z)
        self.assertEqual(result.percentile_rank, Decimal("1"))

    def test_pipeline_rejects_untraceable_peer_values(self):
        case = load_case()
        case["peer_benchmark"] = {
            "metric": "receivables_contract_assets_to_total_assets_2020",
            "eligible_peer_values": ["0.10", "0.20", "0.30", "0.40", "0.50", "0.55", "0.70"],
            "min_sample_size": 10,
        }
        with self.assertRaises(ValueError):
            run_case(case)


class FactCheckerTests(unittest.TestCase):
    def setUp(self):
        self.claims = json.loads((ROOT / "cases" / "meichen_2020" / "report_claims.json").read_text(encoding="utf-8"))
        self.ledger = json.loads((ROOT / "cases" / "meichen_2020" / "reference_ledger.json").read_text(encoding="utf-8"))
        self.features = run_case(load_case())["features"]
        self.result = check_claims(self.claims, self.ledger, self.features)

    def test_claim_count(self):
        self.assertEqual(self.result["checked_claims"], 6)

    def test_rounded_revenue_passes(self):
        row = next(item for item in self.result["results"] if item["claim_id"] == "C001")
        self.assertEqual(row["status"], "PASS")

    def test_ambiguous_net_profit_flagged(self):
        codes = {item["code"] for item in self.result["issues"] if item["claim_id"] == "C002"}
        self.assertIn("METRIC_AMBIGUOUS", codes)

    def test_cash_flow_rounding_passes(self):
        row = next(item for item in self.result["results"] if item["claim_id"] == "C003")
        self.assertEqual(row["status"], "PASS")

    def test_ratio_recalculation_passes(self):
        row = next(item for item in self.result["results"] if item["claim_id"] == "C004")
        self.assertEqual(row["status"], "PASS")

    def test_wrong_page_flagged(self):
        codes = {item["code"] for item in self.result["issues"] if item["claim_id"] == "C005"}
        self.assertIn("CITATION_MISMATCH", codes)

    def test_wrong_direction_flagged(self):
        codes = {item["code"] for item in self.result["issues"] if item["claim_id"] == "C006"}
        self.assertIn("DIRECTION_CONFLICT", codes)

    def test_unsupported_claim_unit_flagged(self):
        claims = copy.deepcopy(self.claims)
        claims["claims"][0]["unit"] = "%"
        result = check_claims(claims, self.ledger, self.features)
        codes = {item["code"] for item in result["issues"] if item["claim_id"] == "C001"}
        self.assertIn("UNIT_ERROR", codes)


class AgentAndEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.analysis = run_case(load_case())
        self.evaluation_map = json.loads((ROOT / "cases" / "meichen_2020" / "evaluation_map.json").read_text(encoding="utf-8"))

    def test_deterministic_explanation_has_boundary(self):
        explanation = deterministic_explanation(self.analysis)
        self.assertIn("不构成", explanation["conclusion_boundary"])

    def test_evaluation_is_single_case_only(self):
        result = evaluate_case(self.analysis, self.evaluation_map)
        self.assertEqual(result["case_count"], 1)
        self.assertIn("不是准确率", result["interpretation_limit"])

    def test_orchestrator_loads_labels_after_analysis(self):
        claims = json.loads((ROOT / "cases" / "meichen_2020" / "report_claims.json").read_text(encoding="utf-8"))
        ledger = json.loads((ROOT / "cases" / "meichen_2020" / "reference_ledger.json").read_text(encoding="utf-8"))
        output = run_fintrace(load_case(), claims=claims, ledger=ledger, evaluation_map=self.evaluation_map)
        self.assertEqual(output["stages"][-1], "ex_post_evaluation")
        self.assertEqual(output["agent_explanation"]["generation_mode"], "deterministic_template")


if __name__ == "__main__":
    unittest.main()
