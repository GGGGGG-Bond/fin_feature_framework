"""Bounded dual-endpoint sampling; evidence files only, no business DB writes."""
import hashlib
import json
import re
import time
import uuid
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
import requests
from fin_feature.reconcile import FIELDS, compare_documents
from fin_feature.storage.raw_store import RawStore, PublishContext, RawStoreError

SINA_URL = 'https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022'
EM_URL = 'https://datacenter-web.eastmoney.com/api/data/v1/get'
SINA_FIELDS = {
    'total_operate_income': ('BIZTOTINCO', '营业总收入'),
    'parent_netprofit': ('PARENETP', '归属于母公司所有者的净利润'),
    'operate_cost': ('BIZCOST', '营业成本'),
    'total_assets': ('TOTASSET', '资产总计'),
    'total_liabilities': ('TOTLIAB', '负债合计'),
    'total_equity': ('RIGHAGGR', '所有者权益(或股东权益)合计'),
}


def validate_scope(secucode, periods):
    if not isinstance(secucode, str) or not re.fullmatch(r'\d{6}\.(SH|SZ)', secucode):
        raise ValueError('sample requires one canonical SH/SZ company')
    if not 1 <= len(periods) <= 2 or len(set(periods)) != len(periods):
        raise ValueError('sample requires one or two distinct annual report dates')
    for period in periods:
        if not re.fullmatch(r'\d{4}-12-31', period):
            raise ValueError('sample supports annual reports only')
        date.fromisoformat(period)


def mapped_row(secucode, period, field, value, evidence, metadata):
    table, basis = FIELDS[field]
    return dict(secucode=secucode, report_date=period, field=field, value=value,
                table_type=table, period_basis=basis, unit='unknown',
                currency='unknown', scope='unknown', comparison_basis_id='unknown',
                alignment_evidence='unknown', evidence_ref=evidence,
                record_ref=evidence + '#' + period + '/' + field,
                mapping_version='source-sample-v1', provider_metadata=metadata)


def map_sina(body, secucode, periods, table, evidence):
    data = json.loads(body, parse_float=str)
    result = data.get('result')
    if not isinstance(result, dict) or type(result.get('status', {}).get('code')) is not int or result['status']['code'] != 0:
        raise ValueError('Sina business status is not success')
    reports = result.get('data', {}).get('report_list')
    if not isinstance(reports, dict):
        raise ValueError('Sina report_list missing')
    rows = []
    for period in periods:
        report = reports.get(period.replace('-', ''))
        if not isinstance(report, dict) or not isinstance(report.get('data'), list):
            raise ValueError('requested annual report absent from bounded Sina page')
        items = {}
        for item in report['data']:
            if not isinstance(item, dict):
                raise ValueError('Sina data item must be an object')
            key = item.get('item_field')
            if key in {v[0] for k,v in SINA_FIELDS.items() if FIELDS[k][0] == table}:
                if key in items:
                    raise ValueError('ambiguous duplicate Sina core field')
                items[key] = item
        metadata = {k:v for k,v in report.items() if k != 'data'}
        for field, (code, title) in SINA_FIELDS.items():
            if FIELDS[field][0] != table:
                continue
            item = items.get(code)
            if item is not None and item.get('item_title') != title:
                raise ValueError('Sina core field title changed: ' + code)
            row = mapped_row(secucode, period, field, item.get('item_value') if item else None, evidence, metadata)
            row['source_field'] = code
            row['currency'] = report.get('rCurrency') or 'unknown'
            row['scope'] = 'consolidated' if report.get('rType') == '合并期末' else 'unknown'
            rows.append(row)
    return rows


def map_eastmoney(body, secucode, periods, table, evidence):
    data = json.loads(body, parse_float=str)
    result = data.get('result')
    if data.get('success') is not True or not isinstance(result, dict):
        raise ValueError('Eastmoney business status is not success')
    records = result.get('data')
    if (type(result.get('pages')) is not int or result['pages'] != 1
            or not isinstance(records, list) or type(result.get('count')) is not int
            or result['count'] != len(records)):
        raise ValueError('Eastmoney sample page incomplete')
    by_date = {}
    for record in records:
        if not isinstance(record, dict) or record.get('SECUCODE') != secucode:
            raise ValueError('Eastmoney company mismatch')
        period = str(record.get('REPORT_DATE', ''))[:10]
        if period not in periods or period in by_date:
            raise ValueError('unexpected or ambiguous Eastmoney period')
        by_date[period] = record
    if set(by_date) != set(periods):
        raise ValueError('requested annual report absent from Eastmoney')
    rows = []
    for period in periods:
        record = by_date[period]
        metadata = {k:record.get(k) for k in ('NOTICE_DATE', 'REPORT_TYPE_CODE', 'DATA_STATE', 'CURRENCY')}
        for field,(expected_table, _) in FIELDS.items():
            if expected_table == table:
                row = mapped_row(secucode, period, field, record.get(field.upper()), evidence, metadata)
                row['source_field'] = field.upper()
                rows.append(row)
    return rows


def write_json(path, value):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')


def run_sample(secucode, periods, output_dir, *, session=None, pause=time.sleep):
    validate_scope(secucode, periods)
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=False)
    manifest = {'schema':'source-sample-v1', 'secucode':secucode, 'periods':periods,
                'status':'failed', 'requests':[], 'scope':'one company, up to two annual dates',
                'full_history':False, 'business_db_written':False}
    owned = session is None
    client = session or requests.Session()
    try:
        store = RawStore(root / 'raw')
        run_id = uuid.uuid4().hex
        documents = {s:dict(schema='reconcile-v1',source=s,upstream=s,records=[])
                     for s in ('eastmoney','sina')}
        for source in ('eastmoney','sina'):
            for table in ('BS','IS'):
                if manifest['requests']:
                    pause(1)
                if source == 'sina':
                    url = SINA_URL
                    params = dict(paperCode=secucode[-2:].lower()+secucode[:6],
                                  source='fzb' if table=='BS' else 'lrb', type='0',page='1',num='20')
                else:
                    url = EM_URL
                    dates = ','.join("'"+p+"'" for p in periods)
                    params = dict(reportName='RPT_DMSK_FN_'+('BALANCE' if table=='BS' else 'INCOME'),
                                  columns='ALL',pageSize=50,pageNumber=1,sortColumns='REPORT_DATE',sortTypes='-1',
                                  filter=f'(SECUCODE="{secucode}")(REPORT_DATE in ({dates}))')
                entry = dict(source=source,table=table,url=url,params=params,outcome='failed')
                manifest['requests'].append(entry)
                response = client.get(url, params=params, timeout=25)
                fetched = datetime.now(timezone.utc).isoformat()
                entry.update(http_status=response.status_code,fetched_at=fetched)
                context = PublishContext(run_id,hashlib.sha256(json.dumps(params,sort_keys=True).encode()).hexdigest(),
                                         uuid.uuid4().hex,1,1,source_name=source,fetched_at=fetched)
                stored = store.publish(context, response.content)
                entry['snapshot'] = asdict(stored)
                entry['outcome'] = 'received'
                write_json(root / (source+'-'+table+'.snapshot.json'), entry)
                if not 200 <= response.status_code < 300:
                    raise ValueError('HTTP error: ' + str(response.status_code))
                evidence = 'raw/' + stored.file_path
                mapper = map_sina if source=='sina' else map_eastmoney
                rows = mapper(response.content,secucode,periods,table,evidence)
                documents[source]['records'].extend(rows)
                entry['outcome'] = 'ok'
        for source, document in documents.items():
            write_json(root / (source+'.json'),document)
        report = compare_documents(documents['eastmoney'],documents['sina'])
        report['input_sha256'] = [hashlib.sha256((root/(s+'.json')).read_bytes()).hexdigest()
                                  for s in ('eastmoney','sina')]
        report['limitations'] = ['unit and revision alignment not independently verified',
                                'different provider endpoints do not prove independent upstream vendors']
        write_json(root/'report.json',report)
        manifest.update(status='collected',counts=report['counts'],total=report['total'],
                        exit_code=0 if report['counts'].get('match',0)==report['total'] else 1)
    except (ValueError, TypeError, KeyError, AttributeError, OSError, requests.RequestException, RawStoreError) as exc:
        manifest.update(error=f'{type(exc).__name__}: {exc}',exit_code=2)
    finally:
        if owned:
            client.close()
        write_json(root/'manifest.json',manifest)
    return manifest


def cmd_source_sample(args):
    import sys
    try:
        result = run_sample(args.secucode,args.report_date,args.output_dir)
        print(json.dumps(result,ensure_ascii=False))
        return result['exit_code']
    except (ValueError, OSError) as exc:
        print(f'Sample failed: {exc}',file=sys.stderr)
        return 2
