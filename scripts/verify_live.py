"""Optional bounded real refresh: one company, at most two adjacent quarters, three requests."""
import argparse
from datetime import date
import json
from pathlib import Path
import re


def validate_scope(secucode,start,end):
    if not re.fullmatch(r'[0-9]{6}\.(SH|SZ)',secucode):
        raise ValueError('one explicit SH/SZ company required')
    periods=[date.fromisoformat(value) for value in (start,end)]
    if any(day.isoformat()!=value or (day.month,day.day) not in ((3,31),(6,30),(9,30),(12,31))
           for day,value in zip(periods,(start,end))):
        raise ValueError('normalized quarter ends required')
    gap=(periods[1].year-periods[0].year)*4+(periods[1].month-periods[0].month)//3
    if gap not in (0,1):
        raise ValueError('range must contain one or two adjacent quarters')


def run_live(output,secucode,start,end):
    validate_scope(secucode,start,end)
    import requests
    from fin_feature.crawler.rate_limiter import RateLimiter
    from fin_feature.delivery import export_report
    from fin_feature.ingestion.http_evidence import EvidenceRequestExecutor
    from fin_feature.ingestion.service import FinancialIngestionCoordinator, FinancialUnitSpec, REPORT_TO_TABLE_TYPE
    from fin_feature.refresh import refresh_current
    from fin_feature.replay import replay_result
    from fin_feature.storage.database import Database
    from fin_feature.storage.raw_store import RawStore
    from fin_feature.storage.repository import Repository
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    db=Database(str(output/'finance.db'));session=requests.Session()
    session.headers.update({'User-Agent':'Mozilla/5.0','Referer':'https://data.eastmoney.com/'})
    try:
        db.init();store=RawStore(output/'raw');repo=Repository(db.conn,raw_store=store)
        executor=EvidenceRequestExecutor(repository=repo,raw_store=store,session=session,
            url='https://datacenter-web.eastmoney.com/api/data/v1/get',timeout=20,max_retries=1,rate_limiter=RateLimiter(2))
        run=FinancialIngestionCoordinator(repo,store,executor,recent_periods=2,max_pages_per_unit=1).run(
            [FinancialUnitSpec(secucode,r,start_date=start,end_date=end) for r in REPORT_TO_TABLE_TYPE])
        refreshed=refresh_current(repo)
        report=export_report(repo,store,output/'report')
        replays=[replay_result(repo,r[0]) for r in db.conn.execute('SELECT result_id FROM feature_compute_log ORDER BY rowid')]
        periods=[r[0] for r in db.conn.execute('SELECT DISTINCT report_date FROM statement_version')]
        in_scope=all(start<=period<=end for period in periods)
        attempts=repo.get_table_count('ingest_attempt')
        result=dict(schema='live-smoke-v1',original_vendor_responses=True,secucode=secucode,
            requested_start=start,requested_end=end,maximum_requests=3,actual_attempts=attempts,
            ingest_status=run.status,failed_units=run.failed_units,financial_units=run.total,
            features=refreshed['recomputed_features'],non_null_features=report['non_null_features'],
            report_exit_code=report['exit_code'],evidence_audit_clean=report['audit']['clean'],
            saved_report_periods=sorted(periods),saved_periods_in_scope=in_scope,
            replay_count=len(replays),replay_matches=sum(r['match'] for r in replays),
            public_visibility_verified=False,output_dir=str(output.resolve()))
        result['exit_code']=0 if (run.exit_code==0 and attempts<=3 and in_scope and replays
                                  and all(r['match'] for r in replays) and report['exit_code']==0) else 1
        (output/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
        (output/'replay.json').write_text(json.dumps(replays,ensure_ascii=False,indent=2)+'\n')
        return result
    finally:
        session.close();db.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',required=True)
    parser.add_argument('--secucode',required=True)
    parser.add_argument('--start',required=True)
    parser.add_argument('--end',required=True)
    args=parser.parse_args()
    try:
        result=run_live(args.output_dir,args.secucode,args.start,args.end)
        print(json.dumps(result,ensure_ascii=False,indent=2));return result['exit_code']
    except (ValueError,OSError,RuntimeError) as exc:
        import sys
        print(f'Live verification failed: {exc}',file=sys.stderr);return 2


if __name__=='__main__':raise SystemExit(main())
