import copy
import unittest
from test_fintrace import load_case
from fintrace.pipeline import run_case
from fintrace.thresholds import assess_peer_value


class PeerGuardTests(unittest.TestCase):
    def setUp(self):
        self.case = load_case()
        self.benchmark = dict(metric='ocf_to_revenue_2020', year=2020,
                              scope='consolidated', definition_version='v1',
                              unit='ratio', peer_group='landscaping', records=[])
        for i in range(10):
            self.benchmark['records'].append({
                **{k: v for k, v in self.benchmark.items() if k != 'records'},
                'record_id': f'synthetic-{i}', 'stock_code': f'{i:06}', 'value': str(i / 100),
                'source': dict(source_type='original_annual_report', verified=True,
                               file_name='synthetic-test-only.pdf', page=1,
                               publication_date='2021-04-01')})
        self.case['peer_benchmark'] = self.benchmark

    def test_valid_records_have_traceable_results(self):
        result = run_case(self.case)['peer_threshold_assessment']
        self.assertEqual(result['status'], 'AVAILABLE')
        self.assertEqual(len(result['accepted_records']), 10)

    def test_future_source_reduces_count(self):
        self.benchmark['records'][0]['source']['publication_date'] = '2022-01-01'
        result = run_case(self.case)['peer_threshold_assessment']
        self.assertEqual(result['sample_size'], 9)
        self.assertEqual(result['status'], 'INSUFFICIENT_PEER_SAMPLE')
        self.assertIn('FUTURE_SOURCE', result['excluded_records'][0]['reasons'])

    def test_bad_evidence_excluded(self):
        for field, value, code in [('source_type', 'updated_report', 'SOURCE_TYPE_NOT_ALLOWED'),
                                   ('verified', False, 'UNVERIFIED_SOURCE'),
                                   ('page', None, 'MISSING_CITATION'),
                                   ('publication_date', None, 'INVALID_PUBLICATION_DATE')]:
            with self.subTest(field=field):
                case = copy.deepcopy(self.case)
                case['peer_benchmark']['records'][0]['source'][field] = value
                result = run_case(case)['peer_threshold_assessment']
                self.assertIn(code, result['excluded_records'][0]['reasons'])

    def test_duplicate_companies_cannot_inflate_sample(self):
        self.benchmark['records'].append(copy.deepcopy(self.benchmark['records'][0]))
        result = run_case(self.case)['peer_threshold_assessment']
        self.assertEqual(result['sample_size'], 9)
        self.assertEqual(len(result['excluded_records']), 2)

    def test_scope_and_target_excluded(self):
        self.benchmark['records'][0]['scope'] = 'parent'
        self.benchmark['records'][1]['stock_code'] = self.case['stock_code']
        result = run_case(self.case)['peer_threshold_assessment']
        self.assertEqual(result['sample_size'], 8)

    def test_nonfinite_and_boolean_rejected(self):
        for value in ['NaN', 'Infinity', True]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                assess_peer_value(value, list(range(10)))

    def test_zero_mad_is_not_zero_risk(self):
        result = assess_peer_value(5, [1] * 10)
        self.assertIsNone(result.robust_z)
