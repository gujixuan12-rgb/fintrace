"""Read only the verified original Qiming PDF pages; reuse the root cutoff gate.

This is a bounded evidence reader and a structured one-claim check, not an LLM
research-report parser. Evaluation labels and later documents are never opened.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

HERE = Path(__file__).resolve().parent
HEADER = '启明信息技术股份有限公司2023年年度报告全文'
VERIFIED_SHA256 = 'a9f276a963e3233aad87ef3eec860af96e99c0819f6ceebcde39df72468ea0d1'
NUMBER = r'(?:[0-9]+|[1-9][0-9]{0,2}(?:,[0-9]{3})+)(?:\.[0-9]+)?'
SPECS = {
    'shares': ('分配预案的股本基数（股）', '股'),
    'dividend_per_ten_shares': ('每10股派息数（元）（含税）', '元/10股'),
    'disclosed_dividend': ('现金分红金额（元）（含税）', '元'),
    'other_page_total': ('利润分配总额为', '万元'),
}
FORBIDDEN = {'post_cutoff_validation', 'corrected_dividend_yuan', 'expected_label',
             'expected_verdict', 'expected_value', 'expected_result', 'ground_truth'}


def compact(text):
    return re.sub(r'\s+', '', text).replace('(', '（').replace(')', '）')


def no_answers(obj):
    if isinstance(obj, dict):
        if FORBIDDEN.intersection(obj):
            raise ValueError('输入夹带评测答案，请移入独立evaluation目录')
        for value in obj.values():
            no_answers(value)
    elif isinstance(obj, list):
        for value in obj:
            no_answers(value)


def strict_day(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('日期缺失或非YYYY-MM-DD')
    return date.fromisoformat(value)


def amount(value):
    if not isinstance(value, str) or not re.fullmatch(NUMBER, value):
        raise ValueError('数值必须是有限非负十进制字符串')
    result = Decimal(value.replace(',', ''))
    if not result.is_finite():
        raise ValueError('非有限数值')
    return result


def project_gate(source, cutoff):
    # Import from the root package selected by --repo-root, not chen_yimin/src.
    from fintrace.models import ClaimRecord, EvidenceRef
    from fintrace.verification.cutoff_filter import filter_record, ELIGIBLE
    ref = EvidenceRef(source_id=source['source_id'],file_name=source['file_name'],
        source_type=source['source_type'],publication_date=source.get('publication_date',''))
    record = ClaimRecord(claim_id='QM-SOURCE-GATE',claim_text='原始证据准入',
                         subject='启明信息技术股份有限公司',period='2023A',evidence=[ref])
    verdict, reasons = filter_record(record, cutoff)
    return verdict == ELIGIBLE, reasons


def row_value(text, label):
    candidates = [line.strip() for line in text.splitlines() if compact(line).startswith(compact(label))]
    if len(candidates) != 1:
        raise ValueError(f'原始字段缺失或不唯一：{label}')
    pattern = r'\s*'.join(re.escape(c) for c in label)
    row = candidates[0].replace('(', '（').replace(')', '）')
    matched = re.fullmatch(pattern + rf'\s*({NUMBER})\s*', row)
    if not matched:
        raise ValueError(f'单位、行列或数值格式不明确：{label}')
    return matched.group(1).replace(',', ''), candidates[0]


def extract_pages(data, audit=None):
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError
    try:
        return _extract_pages(PdfReader(io.BytesIO(data)), audit)
    except PdfReadError as exc:
        raise ValueError(f'PDF无法解析：{exc}') from exc


def _extract_pages(reader, audit=None):
    if len(reader.pages) != 237:
        raise ValueError('PDF页数不是已核验的237页')
    pages = {}
    for p in [51,223]:
        if audit is not None:
            audit['pdf_content_read'] = True
            audit['pdf_pages_read'].append(p)
        text = reader.pages[p-1].extract_text(extraction_mode='layout') or ''
        lines = [line for line in text.splitlines() if line.strip()]
        if not lines or compact(lines[0]) != HEADER or compact(lines[-1]) != str(p):
            raise ValueError(f'第{p}页公司、年度或印刷页码不匹配')
        pages[p] = text
    if compact(pages[51]).count('本报告期利润分配及资本公积金转增股本情况') != 1:
        raise ValueError('第51页预案表标题不唯一')
    if compact(pages[223]).count('2、利润分配情况') != 1:
        raise ValueError('第223页利润分配标题不唯一')
    fields = {}
    for key,(label,unit) in SPECS.items():
        if key == 'other_page_total':
            section = re.split(r'2、\s*利润分配情况',pages[223],maxsplit=1)[1]
            matches = list(re.finditer(r'利润\s*分配总额为\s*(' + NUMBER + r')\s*万元(?=[，,。\s]|$)',section))
            if len(matches) != 1:
                raise ValueError('第223页利润分配总额缺失或不唯一')
            value,quote,p = matches[0].group(1).replace(',',''),matches[0].group(0),223
        else:
            value,quote = row_value(pages[51],label)
            p = 51
        amount(value)
        fields[key] = dict(value=value,unit=unit,printed_page=p,pdf_page=p,quote=quote,
            period='2023A',scope='上市公司向全体股东的利润分配预案',
            tax_basis='不适用' if key == 'shares' else '含税',
            value_status='reported_observation_not_automatically_validated')
    if not amount(fields['shares']['value']) or amount(fields['shares']['value']) % 1:
        raise ValueError('股本基数须为正整数股')
    # Cross-page corroboration for the one supplied claim; not a total-amount test.
    matches = list(re.finditer(r'每\s*10\s*股派发现金红利\s*(' + NUMBER + r')\s*元\s*（含税）',pages[223]))
    if len(matches) != 1 or amount(matches[0].group(1)) != amount(fields['dividend_per_ten_shares']['value']):
        raise ValueError('两页每10股派息含税口径无法相互印证')
    fields['dividend_per_ten_shares']['corroborating_evidence'] = dict(
        pdf_page=223,printed_page=223,quote=matches[0].group(0))
    return pages,fields


def check_claim(claim, fields):
    """One supported template only; unknown semantics return insufficient evidence."""
    no_answers(claim)
    if (claim.get('subject') != '启明信息技术股份有限公司'
            or claim.get('period') != '2023A'
            or claim.get('metric') != 'dividend_per_ten_shares'
            or claim.get('unit') != '元/10股'
            or claim.get('tax_basis') != '含税'
            or claim.get('distribution_status') != 'proposed'
            or claim.get('scope') != '上市公司向全体股东的利润分配预案'):
        return dict(status='INSUFFICIENT_INFORMATION',reason='该主张主体、年度、单位或预案口径不在本工具支持范围',flag_as_error=False)
    template = r'启明信息在2023年年度报告披露的利润分配预案为每10股派发现金红利(' + NUMBER + r')元（含税）。'
    match = re.fullmatch(template,compact(claim.get('claim_text','')))
    if match is None or amount(match.group(1)) != amount(claim.get('value')):
        return dict(status='INSUFFICIENT_INFORMATION',reason='文字主张与结构化数值或预案语义不一致',flag_as_error=False)
    observed=fields['dividend_per_ten_shares']
    supported=amount(claim['value'])==amount(observed['value'])
    return dict(status='SUPPORTED' if supported else 'VALUE_MISMATCH',
        reason='所核查每10股派息预案与原始两页证据一致' if supported else '每10股派息预案与原始证据数值不一致',
        flag_as_error=not supported,claim_id=claim['claim_id'],observed_value=observed['value'],
        claimed_value=claim['value'],unit=observed['unit'],source_pages=[51,223],
        limitation='仅检查此条主张；不将同报告其他金额的差异传播给该主张，不评价实际派付。')


def run(payload, claim, pdf_path, gate=project_gate):
    log=[]
    result=dict(status='INSUFFICIENT_INFORMATION',pdf_content_read=False,pdf_pages_read=[],
                file_accesses=[],tool_calls=log,observations={},claim_check=None)
    try:
        no_answers(payload)
        no_answers(claim)
        if payload.get('company')!='启明信息技术股份有限公司' or payload.get('stock_code')!='002232':
            raise ValueError('不支持的公司')
        cutoff=payload['prediction_cutoff_date']
        strict_day(cutoff)
        source=payload['pre_cutoff_source']
        if source.get('source_type')!='original_annual_report' or source.get('source_version')!='original':
            raise ValueError('只允许已核验原始年报')
        strict_day(source.get('publication_date'))
        eligible,reasons=gate(source,cutoff)
        log.append(dict(tool='fintrace.verification.cutoff_filter.filter_record',eligible=eligible,reasons=reasons))
        if not eligible:
            result.update(status='NOT_ELIGIBLE',reason='；'.join(reasons))
            return result
        proof=source['publication_date_verification']
        if proof.get('status')!='official_page_verified' or proof.get('date')!=source['publication_date']:
            raise ValueError('正式披露日期未核验或与来源记录不一致')
        if source['publication_date']!='2024-04-13':
            raise ValueError('该原始文件的披露日期与官方核验记录不一致')
        if source.get('source_url')!='https://static.cninfo.com.cn/finalpage/2024-04-13/1219596126.PDF':
            raise ValueError('原始公告附件地址与核验清单不一致')
        if source.get('sha256')!=VERIFIED_SHA256 or source.get('bytes')!=4369829:
            raise ValueError('SHA256或文件大小不是已核验的原始年报版本')
        path=Path(pdf_path)
        data=path.read_bytes()
        result['file_accesses'].append(dict(file_name=path.name,purpose='hash_and_size_verification'))
        digest=hashlib.sha256(data).hexdigest()
        if digest!=source['sha256'] or len(data)!=source['bytes']:
            raise ValueError('本地PDF哈希或大小不匹配，拒绝解析')
        log.append(dict(tool='sha256',actual=digest,bytes=len(data),matched=True))
        pages,fields=extract_pages(data, audit=result)
        result.update(page_count=237,source_sha256=digest)
        log.append(dict(tool='pypdf_extract_text',source_id=source['source_id'],pdf_pages=[51,223],printed_pages=[51,223]))
        supplied=payload['observations']
        if set(supplied)!=set(SPECS):
            raise ValueError('人工摘录字段集合不匹配')
        for key,field in fields.items():
            old=supplied[key]
            if old['unit']!=field['unit'] or old['printed_page']!=field['printed_page'] or amount(old['value'])!=amount(field['value']):
                raise ValueError(f'输入摘录与原始PDF不一致：{key}')
            field.update(source_id=source['source_id'],source_file=source['file_name'],
                source_url=source['source_url'],publication_date=source['publication_date'],
                prediction_cutoff_date=cutoff,available_before_cutoff=True)
        checked=check_claim(claim,fields)
        fingerprint=json.dumps(dict(input=payload,claim=claim,pdf_hash=digest),ensure_ascii=False,sort_keys=True).encode('utf-8')
        run_id=f"FT-{cutoff.replace('-','')}-{hashlib.sha256(fingerprint).hexdigest()[:10]}"
        result.update(status='EVIDENCE_READY',run_log_id=run_id,observations=fields,claim_check=checked,
            page_excerpts=[dict(pdf_page=p,printed_page=p,text=(pages[p][pages[p].index('本报告期利润分配及资本公积金转增股本情况'):pages[p].index('十一、公司股权激励')] if p==51 else pages[p][pages[p].index('2、利润分配情况'):]).strip()) for p in [51,223]],
            evidence_refs=[dict(source_id=source['source_id'],file_name=source['file_name'],
                source_type=source['source_type'],publication_date=source['publication_date'],
                page=p,quoted_text=(fields['dividend_per_ten_shares']['quote'] if p==51 else fields['dividend_per_ten_shares']['corroborating_evidence']['quote']),
                available_before_cutoff=True) for p in [51,223]])
        log.append(dict(tool='check_structured_dividend_claim',result=checked))
    except (ValueError,KeyError,OSError,TypeError,InvalidOperation) as exc:
        result['status']='INSUFFICIENT_INFORMATION'
        result['reason']=str(exc)
        log.append(dict(tool='evidence_reader',error=str(exc)))
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo-root',type=Path,default=HERE.parents[2])
    parser.add_argument('--pdf',type=Path,required=True)
    parser.add_argument('--input',type=Path,default=HERE/'qiming_2023_dividend.json')
    parser.add_argument('--claim',type=Path,default=HERE/'claim_input.json')
    parser.add_argument('--out-dir',type=Path,default=HERE/'out')
    args=parser.parse_args()
    root=args.repo_root.resolve()
    if not (root/'src/fintrace/verification/cutoff_filter.py').is_file():
        parser.error('--repo-root须指向fintrace仓库根目录，而不是contributions/chen_yimin')
    sys.path.insert(0,str(root/'src'))
    from fintrace.verification import cutoff_filter
    if Path(cutoff_filter.__file__).resolve() != (root/'src/fintrace/verification/cutoff_filter.py').resolve():
        parser.error('加载了非指定根工程的fintrace包，请使用独立进程')
    payload=json.loads(args.input.read_text(encoding='utf-8'))
    claim=json.loads(args.claim.read_text(encoding='utf-8'))
    result=run(payload,claim,args.pdf)
    result['input_files_read']=[args.input.name,args.claim.name]
    result['evaluation_files_read']=[]
    args.out_dir.mkdir(parents=True,exist_ok=True)
    (args.out_dir/'evidence_reading.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'status':result['status'],'pages':result['pdf_pages_read'],'claim_check':result['claim_check'],'reason':result.get('reason')},ensure_ascii=False))
    return 0 if result['status']=='EVIDENCE_READY' and result['claim_check']['status']=='SUPPORTED' else 2


if __name__=='__main__':
    raise SystemExit(main())
