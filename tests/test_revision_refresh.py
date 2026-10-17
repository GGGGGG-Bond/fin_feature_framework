"""Revisions must invalidate dependent periods and preserve immutable replay."""
import json
import sqlite3

import pytest

from fin_feature.refresh import refresh_current
from fin_feature.replay import replay_result
from fin_feature.features import engine
from test_versioned_pipeline import seed, rows, latest, COMPANY, DATE, CommitFails


@pytest.fixture
def declared_comparable(monkeypatch):
    # Only this controlled fixture declares verified cross-period semantics.
    # Real source records retain unknown semantics and remain blocked.
    original = engine._check_period_comparability
    def fixture_gate(current, comparison, label):
        for record in (current, comparison):
            if record:
                for source in record.get('__sources__', {}).values():
                    source.comparable = True
        return original(current, comparison, label)
    monkeypatch.setattr(engine, '_check_period_comparability', fixture_gate)


def setup_history(repo):
    for period, amount in [('2023-03-31',250),('2023-09-30',200),('2023-12-31',450),
                           ('2024-03-31',300),('2024-06-30',600)]:
        seed(repo,'income_statement',period,total_operate_income=amount)
    seed(repo, company='000002.SZ')


def test_comparison_revision_updates_yoy_and_multistep_qoq(repo,declared_comparable):
    setup_history(repo)
    first=refresh_current(repo)
    assert first['recomputed_features']==72
    assert latest(repo,'revenue_yoy','2024-03-31')['feature_value']==pytest.approx(0.2)
    assert latest(repo,'revenue_qoq','2024-03-31')['feature_value']==pytest.approx(0.2)
    old_logs=rows(repo,'feature_compute_log')
    unrelated=[r for r in old_logs if r['secucode']=='000002.SZ']
    old_qoq=next(r for r in old_logs if r['feature_name']=='revenue_qoq' and r['report_date']=='2024-03-31')
    with repo.unit_of_work():
        repo.upsert_feature(COMPANY,'2024-03-31','legacy_fixture',7,'fixture',None)
        repo.upsert_feature(COMPANY,'2024-03-31','other_fixture',8,'fixture','future-definition')
    revision=seed(repo,'income_statement','2023-09-30',total_operate_income=180)
    # Immediate invalidation happens before refresh, across dependent report periods.
    assert latest(repo,'revenue_qoq','2024-03-31') is None
    assert repo.get_clean_record('income_statement',COMPANY,'2023-09-30') is None
    assert rows(repo,'feature_compute_log')==old_logs
    assert latest(repo,'legacy_fixture','2024-03-31')['feature_value']==7
    assert latest(repo,'other_fixture','2024-03-31')['feature_value']==8
    refreshed=refresh_current(repo)
    assert refreshed['changed_companies']==[COMPANY]
    assert refreshed['recomputed_features']==60
    new=latest(repo,'revenue_qoq','2024-03-31')
    assert new['feature_value']==pytest.approx(300/(450-180)-1)
    assert revision in json.loads(new['input_version_ids'])
    assert [r for r in rows(repo,'feature_compute_log') if r['secucode']=='000002.SZ']==unrelated
    assert rows(repo,'feature_compute_log')[:len(old_logs)]==old_logs
    assert replay_result(repo,old_qoq['result_id'])['match'] is True
    seed(repo,'income_statement','2023-03-31',total_operate_income=200)
    refresh_current(repo)
    assert latest(repo,'revenue_yoy','2024-03-31')['feature_value']==pytest.approx(0.5)


def test_refresh_without_change_is_idempotent(repo):
    seed(repo)
    assert refresh_current(repo)['recomputed_features']==12
    before=rows(repo,'feature_compute_log')
    assert refresh_current(repo)['recomputed_features']==0
    assert rows(repo,'feature_compute_log')==before


def test_dropped_revision_never_leaves_dependent_old_value(repo,declared_comparable):
    setup_history(repo);refresh_current(repo)
    seed(repo,'income_statement','2023-03-31',total_operate_income=None,parent_netprofit=None)
    refresh_current(repo)
    assert repo.get_clean_record('income_statement',COMPANY,'2023-03-31') is None
    result=latest(repo,'revenue_yoy','2024-03-31')
    assert result['feature_value'] is None
    assert result['not_computable_reason'] in ('missing_drop','missing_comparison_period')


@pytest.mark.parametrize('boundary',['log','commit'])
def test_refresh_failure_rolls_back_clean_and_compute_together(repo,db,monkeypatch,boundary):
    seed(repo);refresh_current(repo)
    seed(repo,weightavg_roe=20)
    tables=['clean_statement','clean_selection','clean_financial_indicator','features','feature_compute_log','clean_log']
    before={t:rows(repo,t) for t in tables}
    if boundary=='commit':
        monkeypatch.setattr(repo,'conn',CommitFails(repo.conn))
    else:
        original=repo.save_compute_log
        def fail(*args,**kwargs):
            original(*args,**kwargs)
            raise sqlite3.OperationalError('after immutable log write')
        monkeypatch.setattr(repo,'save_compute_log',fail)
    with pytest.raises(sqlite3.OperationalError):refresh_current(repo)
    from fin_feature.storage.database import get_connection
    from fin_feature.storage.repository import Repository
    fresh=get_connection(db.db_path)
    assert {t:rows(Repository(fresh),t) for t in tables}==before
    fresh.close()


def test_protected_same_key_does_not_force_perpetual_refresh(repo):
    seed(repo)
    with repo.unit_of_work():
        repo.upsert_feature(COMPANY,DATE,'gross_margin_feat',99,'profitability',None)
    assert refresh_current(repo)['recomputed_features']==11
    before=rows(repo,'feature_compute_log')
    assert refresh_current(repo)['recomputed_features']==0
    assert rows(repo,'feature_compute_log')==before
    assert latest(repo,'gross_margin_feat')['feature_value']==99


def test_revision_rollback_restores_original_current_outputs(repo):
    seed(repo);refresh_current(repo)
    before={t:rows(repo,t) for t in ('statement_version','clean_selection','clean_financial_indicator','features')}
    with pytest.raises(RuntimeError,match='abort revision'):
        with repo.unit_of_work():
            seed(repo,weightavg_roe=20)
            assert latest(repo,'gross_margin_feat') is None
            raise RuntimeError('abort revision')
    assert {t:rows(repo,t) for t in before}==before


def test_revision_demo_runs_without_network_and_preserves_all_history(tmp_path,monkeypatch):
    import requests
    from examples.revision_flow import run_revision_demo
    def forbidden(*args,**kwargs):raise AssertionError('offline demo attempted network')
    monkeypatch.setattr(requests.Session,'request',forbidden)
    result=run_revision_demo(tmp_path/'demo')
    assert result['artificial_fixture'] and result['simulated_timestamps']
    assert result['network_requests']==0
    assert result['earlier_gross_margin']==0.4 and result['later_gross_margin']==0.8
    assert result['replay_count']==result['replay_matches']==48
    assert result['unknown_public_selected']==0
    assert len(result['batches'])==2
    assert all(b['status']=='success' and b['audit_exit_code']==0 for b in result['batches'])
    assert len(list((tmp_path/'demo'/'raw').rglob('*.body.gz')))==6


def test_refresh_cli_persists_results_and_is_recoverable(repo,db,tmp_path,capsys):
    from argparse import Namespace
    from fin_feature.refresh import cmd_refresh
    seed(repo)
    cfg=tmp_path/'config.json';cfg.write_text(json.dumps({'database':{'path':db.db_path}}))
    assert cmd_refresh(Namespace(config=str(cfg)))==0
    result=json.loads(capsys.readouterr().out)
    assert result['recomputed_features']==12
    assert len(rows(repo,'features'))==12
    assert cmd_refresh(Namespace(config=str(cfg)))==0
    assert json.loads(capsys.readouterr().out)['recomputed_features']==0


def test_refresh_cli_invalid_rules_do_not_create_database(tmp_path,capsys):
    from argparse import Namespace
    from fin_feature.refresh import cmd_refresh
    path=tmp_path/'absent.db'
    cfg=tmp_path/'invalid.json';cfg.write_text(json.dumps({'database':{'path':str(path)},
        'cleaning':{'missing_strategy':{'parent_netprofit':'invent'}}}))
    assert cmd_refresh(Namespace(config=str(cfg)))==2
    assert 'Refresh failed' in capsys.readouterr().err
    assert not path.exists()
