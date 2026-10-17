"""Artificial offline fixture -> real ingestion/storage -> clean -> features -> replay."""
import argparse
import json
from pathlib import Path

import requests

from fin_feature.cleaning.pipeline import run_cleaning
from fin_feature.features.engine import compute_all
from fin_feature.ingestion.http_evidence import EvidenceRequestExecutor
from fin_feature.ingestion.service import FinancialIngestionCoordinator, FinancialUnitSpec, REPORT_TO_TABLE_TYPE
from fin_feature.replay import replay_result
from fin_feature.storage.database import Database
from fin_feature.storage.raw_store import RawStore
from fin_feature.storage.repository import Repository
from fin_feature.storage.raw_audit import audit_raw


class ArtificialSession:
    """No sockets or real data: clearly labeled, deterministic artificial financial records."""
    def get(self, url, *, params, timeout):
        table = REPORT_TO_TABLE_TYPE[params['reportName']]
        records = []
        for year, scale in ((2025, 1.2), (2024, 1.0)):
            row = {'SECUCODE':'000001.SZ', 'SECURITY_CODE':'000001', 'SECURITY_NAME_ABBR':'ARTIFICIAL_FIXTURE',
                   'NOTICE_DATE':f'{year}-04-20',
                   ('REPORTDATE' if table=='financial_indicator' else 'REPORT_DATE'):f'{year}-03-31'}
            if table == 'balance_sheet':
                row.update(TOTAL_ASSETS=1000*scale,TOTAL_LIABILITIES=600*scale,TOTAL_EQUITY=400*scale,CURRENT_RATIO=200)
            else:
                row.update(TOTAL_OPERATE_INCOME=100*scale,PARENT_NETPROFIT=20*scale)
                if table == 'financial_indicator':
                    row.update(WEIGHTAVG_ROE=16.75,XSMLL=40)
            records.append(row)
        response = requests.Response()
        response.status_code = 200
        response.encoding = 'utf-8'
        response.headers['Content-Type'] = 'application/json'
        response._content = json.dumps({'success':True,'result':{'pages':1,'count':len(records),'data':records}}).encode()
        return response


def run_demo(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)  # A fresh isolated DB, never an existing business directory.
    db = Database(str(output/'finance.db'))
    try:
        db.init()
        store = RawStore(output/'raw')
        repo = Repository(db.conn, raw_store=store)
        executor = EvidenceRequestExecutor(repository=repo,raw_store=store,session=ArtificialSession(),
                                           url='https://offline.invalid/financial',max_retries=1)
        result = FinancialIngestionCoordinator(repo,store,executor,recent_periods=2).run(
            [FinancialUnitSpec('000001.SZ',report) for report in REPORT_TO_TABLE_TYPE])
        if result.exit_code:
            raise RuntimeError('offline ingestion failed')
        run_cleaning(repo,input_mode='versions')
        feature_count = compute_all(repo,input_mode='versions')
        ids = [r[0] for r in db.conn.execute('SELECT result_id FROM feature_compute_log ORDER BY result_id')]
        replays = [replay_result(repo,result_id) for result_id in ids]
        audit = audit_raw(db.conn,store,run_id=result.run_id)
        summary = {'artificial_fixture':True,'network_requests':0,'output_dir':str(output.resolve()),
                   'ingest_status':result.status,'financial_units':result.total,'features':feature_count,
                   'replay_count':len(replays),'replay_matches':sum(r['match'] for r in replays),
                   'audit_exit_code':audit.exit_code(),'replay_scope':'saved-inputs/current-code'}
        (output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
        (output/'replay.json').write_text(json.dumps(replays,ensure_ascii=False,indent=2)+'\n')
        return summary
    finally:
        db.close()


def main():
    parser=argparse.ArgumentParser(description='Artificial offline financial flow; no network, new output directory only')
    parser.add_argument('--output-dir',required=True)
    args=parser.parse_args()
    summary=run_demo(args.output_dir)
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    return 0 if summary['replay_count'] and summary['replay_matches']==summary['replay_count'] and summary['audit_exit_code']==0 else 1


if __name__=='__main__':
    raise SystemExit(main())
