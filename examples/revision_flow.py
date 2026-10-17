"""Artificial revision timeline through the actual evidence/clean/feature pipeline."""
import argparse
import json
from pathlib import Path

from examples.offline_flow import ArtificialSession
from fin_feature.asof import query_asof
from fin_feature.ingestion.http_evidence import EvidenceRequestExecutor
from fin_feature.ingestion.service import FinancialIngestionCoordinator, FinancialUnitSpec, REPORT_TO_TABLE_TYPE
from fin_feature.refresh import refresh_current
from fin_feature.replay import replay_result
from fin_feature.storage.database import Database
from fin_feature.storage.raw_store import RawStore
from fin_feature.storage.repository import Repository
from fin_feature.storage.raw_audit import audit_raw


class RevisionSession(ArtificialSession):
    def get(self, url, *, params, timeout):
        response = super().get(url, params=params, timeout=timeout)
        payload = response.json()
        if REPORT_TO_TABLE_TYPE[params['reportName']] == 'financial_indicator':
            for row in payload['result']['data']:
                if row['REPORTDATE'] == '2025-03-31':
                    row.update(NOTICE_DATE='2025-05-20', XSMLL=80)
        response._content = json.dumps(payload).encode()
        return response


def run_revision_demo(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    db = Database(str(output/'finance.db'))
    try:
        db.init()
        store = RawStore(output/'raw')
        repo = Repository(db.conn, raw_store=store)
        batches = []
        for session, timestamp in [(ArtificialSession(), '2025-05-01T08:00:00Z'),
                                   (RevisionSession(), '2025-06-01T08:00:00Z')]:
            clock = lambda: timestamp  # Explicitly simulated receipt time; no real HTTP.
            executor = EvidenceRequestExecutor(repository=repo, raw_store=store, session=session,
                url='https://offline.invalid/artificial', max_retries=1, clock=clock)
            run = FinancialIngestionCoordinator(repo, store, executor, recent_periods=2, clock=clock).run(
                [FinancialUnitSpec('000001.SZ', report) for report in REPORT_TO_TABLE_TYPE])
            if run.exit_code:
                raise RuntimeError('artificial ingestion failed')
            refreshed = refresh_current(repo)
            audit = audit_raw(db.conn, store, run_id=run.run_id)
            batches.append(dict(run_id=run.run_id, status=run.status, units=run.total,
                                audit_exit_code=audit.exit_code(), refresh=refreshed))
        early = query_asof(repo, '2025-05-15', include_features=True)
        late = query_asof(repo, '2025-06-15', include_features=True)
        public = query_asof(repo, '2025-06-15', view='public')
        ids = [r[0] for r in db.conn.execute('SELECT result_id FROM feature_compute_log ORDER BY rowid')]
        replays = [replay_result(repo, result_id) for result_id in ids]
        def gross(report):
            return next(r['feature_value'] for r in report['features']
                        if r['feature_name']=='gross_margin_feat' and r['report_date']=='2025-03-31')
        summary = dict(artificial_fixture=True, simulated_timestamps=True, network_requests=0,
            batches=batches, earlier_gross_margin=gross(early), later_gross_margin=gross(late),
            replay_count=len(replays), replay_matches=sum(r['match'] for r in replays),
            unknown_public_selected=public['selected_count'],
            public_limit='Coordinator leaves revision type unknown; no fabricated public visibility.',
            output_dir=str(output.resolve()))
        for name, value in [('summary',summary),('early',early),('late',late),('public',public),('replay',replays)]:
            (output/(name+'.json')).write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')
        return summary
    finally:
        db.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',required=True)
    result=run_revision_demo(parser.parse_args().output_dir)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if (result['earlier_gross_margin']==0.4 and result['later_gross_margin']==0.8
                 and result['replay_count']==result['replay_matches']==48
                 and all(b['audit_exit_code']==0 for b in result['batches'])) else 1


if __name__=='__main__':
    raise SystemExit(main())
