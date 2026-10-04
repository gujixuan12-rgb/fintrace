"""Evaluate a completed evidence-reader result; never import this in detection."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--result', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    labels_path = Path(__file__).with_name('expected_labels.json')
    labels = json.loads(labels_path.read_text(encoding='utf-8'))
    result = json.loads(args.result.read_text(encoding='utf-8'))
    actual = result.get('claim_check') or {}
    checks = {
        'evidence_ready': result.get('status') == 'EVIDENCE_READY',
        'expected_claim': actual.get('claim_id') == labels['claim_id'],
        'correct_claim_supported': actual.get('status') == labels['expected_verdict'],
        'no_false_error': actual.get('flag_as_error') is labels['expected_flag_as_error'],
        'original_hash': result.get('source_sha256') == labels['source_sha256'],
        'bounded_pdf_pages': result.get('pdf_pages_read') == [51, 223],
        'no_evaluation_files_in_detector_log': result.get('evaluation_files_read') == [],
        'root_cutoff_gate_used': any(
            item.get('tool') == 'fintrace.verification.cutoff_filter.filter_record'
            and item.get('eligible') is True for item in result.get('tool_calls', [])
        ),
    }
    passed = all(checks.values())
    report = {
        'passed': passed,
        'checks': checks,
        'run_log_id': result.get('run_log_id'),
        'correct_claim_count': 1,
        'false_error_count': int(actual.get('flag_as_error') is True) if checks['expected_claim'] else None,
        'actual_verdict': actual.get('status'),
        'scope': '独立脚本对一个结构化正确主张的核查；不代表完整系统或大模型效果',
        'detector_result_file': args.result.name,
        'evaluation_label_file': labels_path.name,
        'separation': '检测进程已结束后，单独进程读取标准答案和检测结果。',
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'passed': passed, 'checks': checks}, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
