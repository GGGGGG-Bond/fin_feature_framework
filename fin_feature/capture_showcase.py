"""Explicit bounded refresh of the showcase: 8 fixed companies, no full market.

Writes actual HTTP bodies outside Git plus field extracts in a new output directory.
It never changes pinned demo snapshots or the caller's business database.
"""
import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import sys
import sqlite3
import time
import uuid

import requests

from fin_feature.crawler.rate_limiter import RateLimiter
from fin_feature.ingestion.http_evidence import EvidenceRequestExecutor
from fin_feature.ingestion.service import FinancialIngestionCoordinator, FinancialUnitSpec, REPORT_TO_TABLE_TYPE
from fin_feature.sample_batch import run_batch, validate_plan
from fin_feature.storage.database import Database
from fin_feature.storage.field_mapping import FIELD_MAPPINGS
from fin_feature.storage.raw_store import RawStore, PublishContext, RawStoreError
from fin_feature.storage.repository import Repository

PLAN = Path(__file__).resolve().parents[1] / 'examples/sample-plans/stage3-eight-companies.json'
PRICE_URL = 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')


def price_rows(payload, symbol):
    if not isinstance(payload, dict) or type(payload.get('code')) is not int or payload['code'] != 0:
        raise ValueError('price provider business failure')
    data = payload.get('data')
    if not isinstance(data, dict) or not isinstance(data.get(symbol), dict):
        raise ValueError('price response data/symbol must be objects')
    rows = data[symbol].get('qfqday')
    if not isinstance(rows, list) or not rows:
        raise ValueError('adjusted prices missing; no unadjusted fallback')
    from fin_feature.addons.returns import _prices
    if any(not isinstance(r, list) or len(r)<3 or not isinstance(r[0],str) for r in rows):
        raise ValueError('price rows require date, open and close')
    bars = [dict(date=r[0], open=r[1], close=r[2]) for r in rows]
    if any(not '2025-05-01' <= r['date'] <= '2025-12-31' for r in bars):
        raise ValueError('price date outside requested scope')
    _prices(dict(schema='captured-adjusted-prices-v1', instruments=[dict(secucode='510300.SH', price_basis='provider_qfq', bars=bars)]))
    return bars


def capture_prices(companies, output, *, session=None, pause=time.sleep):
    root = Path(output);root.mkdir(parents=True, exist_ok=False)
    store = RawStore(root / 'raw')
    own_session = session is None
    client = session or requests.Session()
    instruments, attempts = [], []
    try:
        for company in [*companies, dict(secucode='510300.SH', name='沪深300ETF')]:
            pause(1)
            code = company['secucode'];symbol = code[-2:].lower()+code[:6]
            params = dict(param=f'{symbol},day,2025-05-01,2025-12-31,640,qfq')
            attempt = dict(secucode=code, url=PRICE_URL, params=params, status='failed')
            attempts.append(attempt)
            try:
                response = client.get(PRICE_URL, params=params, timeout=25)
                fetched = datetime.now(timezone.utc).isoformat()
                context = PublishContext(uuid.uuid4().hex, hashlib.sha256(json.dumps(params,sort_keys=True).encode()).hexdigest(),
                    uuid.uuid4().hex, 1, 1, source_name='tencent', fetched_at=fetched, http_status=response.status_code)
                stored = store.publish(context, response.content)
                attempt.update(fetched_at=fetched, http_status=response.status_code, raw_sha256=stored.raw_hash, file_path=stored.file_path)
                response.raise_for_status()
                bars = price_rows(response.json(), symbol)
                instruments.append(dict(secucode=code, name=company['name'], source='tencent', price_basis='provider_qfq',
                    url=response.url, fetched_at=fetched, raw_sha256=stored.raw_hash, bars=bars))
                attempt['status']='collected'
            except (requests.RequestException, ValueError, KeyError, IndexError, TypeError, OSError, RawStoreError) as exc:
                attempt.update(error_type=type(exc).__name__, detail=str(exc))
        result = dict(schema='captured-adjusted-prices-v1', start='2025-05-01', end='2025-12-31', instruments=instruments)
        save(root/'prices.json',result);save(root/'attempts.json',attempts)
        return result, attempts
    finally:
        if own_session:client.close()


def capture(output):
    plan=json.loads(PLAN.read_bytes());companies,periods=validate_plan(plan)
    if len(companies)!=8 or periods!=['2024-12-31','2023-12-31'] or any('reuse_sample' in c for c in companies):
        raise ValueError('capture requires the reviewed fixed eight-company plan, without reuse')
    root=Path(output);root.mkdir(parents=True,exist_ok=False)
    summary=dict(schema='live-showcase-capture-v1',status='failed',planned_companies=8,planned_primary_units=24,
                 planned_dual_requests=32,planned_price_requests=9,original_body_files_outside_git=True)
    try:
        dual=run_batch(plan,root/'dual')
        captured=[]
        for company in dual['companies']:
            if company['status']!='collected':continue
            path=Path(company['sample_dir']);manifest=json.loads((path/'manifest.json').read_bytes())
            captured.append(dict(secucode=company['secucode'],name=company['name'],group=company['group'],
                providers={s:json.loads((path/(s+'.json')).read_bytes()) for s in ('eastmoney','sina')},
                requests=[{k:r[k] for k in ('source','table','url','params','fetched_at','http_status')}|dict(raw_sha256=r['snapshot']['raw_hash']) for r in manifest['requests']]))
        save(root/'dual-source.json',dict(schema='captured-dual-fields-v1',periods=periods,companies=captured,
            original_responses_in_repository=False,upstream_independence_verified=False))
        primary=root/'primary';primary.mkdir();db=Database(str(primary/'finance.db'))
        client=requests.Session()
        try:
            db.init();store=RawStore(primary/'raw');repo=Repository(db.conn,raw_store=store)
            client.headers.update({'User-Agent':'Mozilla/5.0','Referer':'https://data.eastmoney.com/'})
            executor=EvidenceRequestExecutor(repository=repo,raw_store=store,session=client,timeout=25,max_retries=1,
                url='https://datacenter-web.eastmoney.com/api/data/v1/get',rate_limiter=RateLimiter(1))
            run=FinancialIngestionCoordinator(repo,store,executor,recent_periods=8,max_pages_per_unit=1).run(
                [FinancialUnitSpec(c['secucode'],r,start_date='2023-03-31',end_date='2024-12-31') for c in companies for r in REPORT_TO_TABLE_TYPE])
            pages=[]
            for snap in db.conn.execute("SELECT s.* FROM raw_snapshot s JOIN ingest_attempt a USING(attempt_id) JOIN ingest_unit u USING(unit_id) WHERE s.validation_status='valid' ORDER BY s.rowid"):
                payload=json.loads(gzip.decompress((store.root/snap['file_path']).read_bytes()))
                rows=[{k:v for k,v in row.items() if k in FIELD_MAPPINGS[snap['report_name']]} for row in payload['result']['data']]
                pages.append(dict(secucode=snap['secucode'],report_name=snap['report_name'],fetched_at=snap['fetched_at'],
                    raw_sha256=snap['raw_content_hash'],request_params=json.loads(snap['request_params']),rows=rows))
            save(root/'financials.json',dict(schema='captured-financial-fields-v1',captured_at=datetime.now(timezone.utc).isoformat(),
                provider='eastmoney',original_responses_in_repository=False,companies=companies,start='2023-03-31',end='2024-12-31',pages=pages,
                note='Selected fields from actual captured bodies; offline envelopes must be labelled reconstructed.'))
            summary.update(primary_status=run.status,primary_pages=len(pages),primary_exit_code=run.exit_code,
                           primary_attempts=db.conn.execute('SELECT COUNT(*) FROM ingest_attempt').fetchone()[0])
        finally:
            client.close();db.close()
        _,attempts=capture_prices(companies,root/'prices')
        summary.update(dual_companies=len(captured),dual_actual_requests=dual['actual_http_requests'],price_attempts=len(attempts),
                       price_successes=sum(a['status']=='collected' for a in attempts))
        summary['status']='success' if run.exit_code==0 and len(captured)==8 and summary['price_successes']==9 else 'partial_failure'
        return summary
    finally:
        save(root/'summary.json',summary)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',required=True)
    parser.add_argument('--run-live',action='store_true',help='Explicitly enable up to 65 bounded public HTTP requests')
    args=parser.parse_args()
    if not args.run_live:parser.error('--run-live is required; normal demo is offline')
    if not sys.platform.startswith('linux'):parser.error('Linux/WSL required for evidence-file publication')
    try:
        result=capture(args.output_dir);print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0 if result['status']=='success' else 1
    except (ValueError,OSError,RuntimeError,KeyError,sqlite3.Error,RawStoreError,requests.RequestException) as exc:
        print(f'Capture failed: {type(exc).__name__}: {exc}',file=sys.stderr);return 2


if __name__=='__main__':raise SystemExit(main())
