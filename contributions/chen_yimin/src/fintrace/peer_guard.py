"""Validate evidence-bearing peer records before any benchmark calculation."""
from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation


def filter_peers(benchmark, case):
    if 'eligible_peer_values' in benchmark:
        raise ValueError('同行裸数值不再受信任，请提供带来源的 records')
    required = ('metric', 'year', 'scope', 'definition_version', 'unit', 'peer_group')
    if any(not benchmark.get(key) for key in required):
        raise ValueError('同行基准缺少指标、年度、口径、定义版本、单位或业务组')
    cutoff = date.fromisoformat(case['as_of_date'])
    if str(benchmark['year']) not in case['financials']:
        raise ValueError('同行年度不在目标财务期间内')
    if not benchmark['metric'].endswith('_' + str(benchmark['year'])):
        raise ValueError('指标年度与同行年度不一致')
    rows = benchmark.get('records', [])
    counts = Counter(str(row.get('stock_code', '')).strip() for row in rows)
    accepted, excluded, values = [], [], []
    for index, row in enumerate(rows):
        reasons = []
        code = str(row.get('stock_code', '')).strip()
        if not code:
            reasons.append('MISSING_COMPANY')
        elif code == str(case['stock_code']):
            reasons.append('TARGET_COMPANY')
        elif counts[code] > 1:
            reasons.append('DUPLICATE_COMPANY')
        for key in required:
            if row.get(key) != benchmark[key]:
                reasons.append(key.upper() + '_MISMATCH')
        source = row.get('source') or {}
        if source.get('source_type') != 'original_annual_report':
            reasons.append('SOURCE_TYPE_NOT_ALLOWED')
        if source.get('verified') is not True:
            reasons.append('UNVERIFIED_SOURCE')
        if not source.get('file_name') or not source.get('page'):
            reasons.append('MISSING_CITATION')
        try:
            if date.fromisoformat(source.get('publication_date', '')) > cutoff:
                reasons.append('FUTURE_SOURCE')
        except (ValueError, TypeError):
            reasons.append('INVALID_PUBLICATION_DATE')
        try:
            raw = row.get('value')
            if isinstance(raw, bool):
                raise ValueError()
            value = Decimal(str(raw))
            if not value.is_finite():
                raise ValueError()
        except (InvalidOperation, ValueError):
            reasons.append('INVALID_VALUE')
        identity = {'record_id': row.get('record_id', f'row-{index + 1}'), 'stock_code': code}
        if reasons:
            excluded.append({**identity, 'reasons': reasons})
        else:
            values.append(value)
            accepted.append({**identity, 'value': str(value), 'source': source})
    return values, accepted, excluded
