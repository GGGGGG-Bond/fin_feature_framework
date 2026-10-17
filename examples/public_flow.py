"""Offline replay of reviewed public facts, using reconstructed API envelopes.

These envelopes are fixtures, not original vendor HTTP responses or fresh pulls.
"""
import argparse
import hashlib
import json
import re
from pathlib import Path

import requests

from fin_feature.delivery import export_report
from fin_feature.ingestion.http_evidence import EvidenceRequestExecutor
from fin_feature.ingestion.service import FinancialIngestionCoordinator, FinancialUnitSpec, REPORT_TO_TABLE_TYPE
from fin_feature.refresh import refresh_current
from fin_feature.replay import replay_result
from fin_feature.storage.database import Database
from fin_feature.storage.raw_store import RawStore
from fin_feature.storage.repository import Repository

FIXTURE = Path(__file__).with_name('public_financials.json')
FIELDS = dict(total_operate_income='TOTAL_OPERATE_INCOME', parent_netprofit='PARENT_NETPROFIT',
              operate_cost='OPERATE_COST',total_assets='TOTAL_ASSETS',
              total_liabilities='TOTAL_LIABILITIES',total_equity='TOTAL_EQUITY')


def load_public_fixture(path):
    fixture=json.loads(Path(path).read_bytes())
    if fixture.get('schema')!='public-financial-fixture-v1' or len(fixture.get('records',[]))!=32:
        raise ValueError('unexpected reviewed public fixture')
    if (fixture.get('currency')!='CNY' or fixture.get('amount_unit')!='yuan'
            or fixture.get('consolidate_scope')!='consolidated' or fixture.get('provider_payload') is not False):
        raise ValueError('public fixture currency/unit/scope or response identity differs')
    root=Path(__file__).resolve().parents[1]
    documents=fixture.get('documents',[])
    hashes={doc['sha256'] for doc in documents}
    if len(hashes)!=3 or len(documents)!=3 or any(row['document_sha256'] not in hashes for row in fixture['records']):
        raise ValueError('unknown or duplicate public document identity')
    for doc in documents:
        reference=(root/doc['reference_file']).resolve()
        if root not in reference.parents:
            raise ValueError('public reference must stay inside checkout')
        body=reference.read_bytes()
        if hashlib.sha256(body).hexdigest()!=doc['reference_sha256']:
            raise ValueError('public reference file hash differs')
        reviewed=json.loads(body)
        if doc['sha256']!=reviewed['document']['sha256'] or doc['url']!=reviewed['document']['url']:
            raise ValueError('public document identity differs')
        if any(row.get('document_url')!=doc['url'] for row in fixture['records'] if row['document_sha256']==doc['sha256']):
            raise ValueError('public fact document link differs')
        frozen=[{k:v for k,v in row.items() if k not in ('document_sha256','document_url')}
                for row in fixture['records'] if row['document_sha256']==doc['sha256']]
        encoded=lambda rows:sorted(json.dumps(row,sort_keys=True,ensure_ascii=False) for row in rows)
        if encoded(frozen)!=encoded(reviewed['records']):
            raise ValueError('public facts differ from pinned reviewed fields')
    return fixture


class PublicFactSession:
    def __init__(self, fixture):
        self.fixture=fixture

    def get(self,url,*,params,timeout):
        match=re.search(r'SECUCODE="([0-9]{6}\.(?:SH|SZ))"',params['filter'])
        if match is None:
            raise ValueError('fixture requires a single explicit company')
        company=match.group(1);table=REPORT_TO_TABLE_TYPE[params['reportName']]
        records={}
        for item in self.fixture['records']:
            if item['secucode']!=company or {'IS':'income_statement','BS':'balance_sheet'}[item['table_type']]!=table:
                continue
            period=item['report_date']
            row=records.setdefault(period,dict(SECUCODE=company,SECURITY_CODE=company[:6],
                SECURITY_NAME_ABBR='PUBLIC_FACT_FIXTURE',REPORT_DATE=period,NOTICE_DATE=None))
            row[FIELDS[item['field']]]=item['value']
        response=requests.Response();response.status_code=200;response.encoding='utf-8'
        response.headers['Content-Type']='application/json'
        response._content=json.dumps(dict(success=True,result=dict(pages=1,count=len(records),
            data=[records[p] for p in sorted(records,reverse=True)]))).encode()
        return response


def run_public_demo(output):
    fixture=load_public_fixture(FIXTURE)
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    db=Database(str(output/'finance.db'))
    try:
        db.init();store=RawStore(output/'raw');repo=Repository(db.conn,raw_store=store)
        executor=EvidenceRequestExecutor(repository=repo,raw_store=store,session=PublicFactSession(fixture),
            url='https://offline.invalid/reconstructed-public-facts',max_retries=1)
        specs=[FinancialUnitSpec(c,r) for c in sorted({i['secucode'] for i in fixture['records']})
               for r,t in REPORT_TO_TABLE_TYPE.items() if t!='financial_indicator']
        run=FinancialIngestionCoordinator(repo,store,executor,recent_periods=2).run(specs)
        if run.exit_code:raise RuntimeError('public fact fixture ingestion failed')
        refreshed=refresh_current(repo)
        report=export_report(repo,store,output/'report',review_summary=Path(__file__).parents[1]/'docs/stage-3/review-result-20261018.json')
        replays=[replay_result(repo,r[0]) for r in db.conn.execute('SELECT result_id FROM feature_compute_log ORDER BY rowid')]
        summary=dict(public_facts=True,reconstructed_api_envelopes=True,original_vendor_responses=False,
            network_requests=0,reviewed_public_facts=len(fixture['records']),financial_units=run.total,
            ingest_status=run.status,features=refreshed['recomputed_features'],non_null_features=report['non_null_features'],
            replay_count=len(replays),replay_matches=sum(r['match'] for r in replays),
            report_exit_code=report['exit_code'],evidence_audit_clean=report['audit']['clean'],
            limitations=fixture['note'],output_dir=str(output.resolve()))
        (output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
        (output/'fixture-provenance.json').write_text(json.dumps(fixture,ensure_ascii=False,indent=2)+'\n')
        (output/'replay.json').write_text(json.dumps(replays,ensure_ascii=False,indent=2)+'\n')
        return summary
    finally:
        db.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output-dir',required=True)
    result=run_public_demo(parser.parse_args().output_dir)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result['features']==result['replay_count']==result['replay_matches']==72 and result['report_exit_code']==0 else 1


if __name__=='__main__':raise SystemExit(main())
