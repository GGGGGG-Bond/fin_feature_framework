"""Stage4: real temporary SQLite/gzip; explicitly declared artificial history."""
import json
from argparse import Namespace

import pytest

from fin_feature.asof import query_asof, cutoff
from fin_feature.constants import REPORT_INCOME_STATEMENT
from fin_feature.storage.hashing import canonical_json

COMPANY = '000001.SZ'
PERIOD = '2023-12-31'


def observe(env, amount, received, notice='2024-03-20', kind='initial', revision=None):
    repo = env.repo
    raw = dict(secucode=COMPANY, report_date=PERIOD, total_operate_income=amount,
               parent_netprofit=20, notice_date=notice)
    with repo.unit_of_work():
        env.new_run()
        unit = env.new_unit(report_name=REPORT_INCOME_STATEMENT)
        captured = env.capture(unit, rows=[raw], fetched_at=received)
        version = repo.create_statement_version(COMPANY, PERIOD, 'income_statement',
            snapshot_id=captured.snapshot_id, run_id=env.run_id, observed_at=received,
            notice_date=notice, revision_type=kind, revision_notice_date=revision,
            standardized_fields=canonical_json({'total_operate_income': amount, 'parent_netprofit':20}),
            raw_fields=canonical_json(raw), row_index=0, evidence_sufficient=0)
    return version, captured.snapshot_id


def chosen(env, when, view='system'):
    return query_asof(env.repo, when, view, COMPANY)


def test_two_clocks_never_backfill_future_revision(evidence_env):
    env=evidence_env
    first,_=observe(env,100,'2024-03-21T08:00:00Z')
    second,_=observe(env,120,'2024-05-11T08:00:00Z',kind='revision',revision='2024-05-10')
    assert chosen(env,'2024-03-20')['selected_count']==0
    assert chosen(env,'2024-03-20','public')['statements'][0]['version_id']==first
    assert chosen(env,'2024-05-10','system')['statements'][0]['version_id']==first
    assert chosen(env,'2024-05-09','public')['statements'][0]['version_id']==first
    assert chosen(env,'2024-05-10','public')['statements'][0]['version_id']==second
    assert chosen(env,'2024-05-11T07:59:59Z')['statements'][0]['version_id']==first
    assert chosen(env,'2024-05-11T16:00:00+08:00')['statements'][0]['version_id']==second


def test_unknown_revision_date_cannot_borrow_first_notice(evidence_env):
    env=evidence_env
    first,_=observe(env,100,'2024-03-21T08:00:00Z')
    second,_=observe(env,120,'2024-05-11T08:00:00Z',kind='revision')
    public=chosen(env,'2024-06-01','public')
    assert public['statements'][0]['version_id']==first
    assert any(d['reason_code']=='unknown_public_time' for d in public['diagnostics'])
    assert chosen(env,'2024-06-01')['selected_count']==1
    # Same notice and no entry_time cannot establish which content was later.
    assert any(d['reason_code']=='ambiguous_same_date_selection' for d in chosen(env,'2024-06-01')['diagnostics'])


def test_production_unknown_public_type_is_excluded(evidence_env):
    observe(evidence_env,100,'2024-03-21T08:00:00Z',kind='unknown')
    report=chosen(evidence_env,'2025-01-01','public')
    assert report['selected_count']==0
    assert {d['reason_code'] for d in report['diagnostics']}=={'unknown_public_time','no_saved_eligible_version'}
    assert chosen(evidence_env,'2025-01-01')['selected_count']==1


def test_a_b_a_uses_observations_not_current_version_flags(evidence_env):
    env=evidence_env
    a,_=observe(env,100,'2024-03-21T08:00:00Z')
    b,_=observe(env,120,'2024-05-11T08:00:00Z',notice='2024-05-10',kind='revision',revision='2024-05-10')
    again,_=observe(env,100,'2024-06-11T08:00:00Z',notice='2024-06-10',kind='revision',revision='2024-06-10')
    assert again==a
    assert env.conn.execute('SELECT COUNT(*) FROM statement_version').fetchone()[0]==2
    for view in ('system','public'):
        assert chosen(env,'2024-04-01',view)['statements'][0]['version_id']==a
        assert chosen(env,'2024-05-20',view)['statements'][0]['version_id']==b
        assert chosen(env,'2024-07-01',view)['statements'][0]['version_id']==a


def test_later_observation_of_old_notice_does_not_downgrade(evidence_env):
    env=evidence_env
    newer,_=observe(env,120,'2024-05-11T08:00:00Z',notice='2024-05-10')
    observe(env,100,'2024-06-11T08:00:00Z')
    assert chosen(env,'2024-07-01')['statements'][0]['version_id']==newer


def test_query_features_is_read_only_even_without_cleaning(evidence_env):
    env=evidence_env
    version,_=observe(env,100,'2024-03-21T08:00:00Z')
    before=env.conn.total_changes
    report=query_asof(env.repo,'2024-04-01',include_features=True)
    assert env.conn.total_changes==before
    assert env.conn.execute('SELECT COUNT(*) FROM clean_statement').fetchone()[0]==0
    assert env.conn.execute('SELECT COUNT(*) FROM feature_compute_log').fetchone()[0]==0
    margin=next(r for r in report['features'] if r['feature_name']=='net_profit_margin')
    assert margin['feature_value']==0.2
    assert {i['record_ref'] for i in margin['inputs'] if i['record_ref']}=={version}
    assert report['current_projections_written'] is False


@pytest.mark.parametrize('time_value', ['2024-03-21 08:00:00', 'invalid', None])
def test_unknown_or_inconsistent_observation_time_is_not_inferred(evidence_env,time_value):
    env=evidence_env
    _,snapshot=observe(env,100,'2024-03-21T08:00:00Z')
    env.conn.execute('UPDATE statement_observation SET observed_at=? WHERE snapshot_id=?',
                     (time_value or 'unknown',snapshot));env.conn.commit()
    report=chosen(env,'2025-01-01')
    assert report['selected_count']==0
    assert any(d['reason_code']=='unknown_observation_time' for d in report['diagnostics'])


def test_missing_old_version_cannot_restore_old_value(evidence_env):
    observe(evidence_env,120,'2024-05-11T08:00:00Z',kind='revision',revision='2024-05-10')
    report=chosen(evidence_env,'2024-04-01','public')
    assert report['statements']==[]
    assert any(d['reason_code']=='no_saved_eligible_version' for d in report['diagnostics'])
    assert report['complete_market_history_verified'] is False


@pytest.mark.parametrize('when,view', [('2024-03-21T08:00:00','system'),('2024-03-21T08:00:00Z','public'),
    ('2024-02-30','public'), ('2024/03/21','system'),('2024-03-21','bad')])
def test_invalid_cutoff_rejected(when,view):
    with pytest.raises(ValueError):cutoff(when,view)


def test_hash_tamper_is_rejected_in_historical_query(evidence_env):
    observe(evidence_env,100,'2024-03-21T08:00:00Z')
    evidence_env.conn.execute("UPDATE statement_version SET standardized_fields='{\"total_operate_income\":999}'")
    evidence_env.conn.commit()
    with pytest.raises(ValueError,match='hash mismatch'):chosen(evidence_env,'2025-01-01')


def test_outer_transaction_is_preserved(evidence_env):
    env=evidence_env
    observe(env,100,'2024-03-21T08:00:00Z')
    env.conn.execute('BEGIN')
    report=chosen(env,'2025-01-01')
    assert report['selected_count']==1
    assert env.conn.in_transaction
    env.conn.rollback()


def test_cli_opens_read_only_and_preserves_database(evidence_env,tmp_path,capsys):
    import yaml
    from fin_feature.asof import cmd_asof
    env=evidence_env
    observe(env,100,'2024-03-21T08:00:00Z')
    path=env.conn.execute('PRAGMA database_list').fetchone()[2]
    cfg=tmp_path/'config.yaml';cfg.write_text(yaml.safe_dump({'database':{'path':path}}))
    # Config keys are resolved by the actual public config loader.
    from fin_feature.config import load_config
    loaded=load_config(str(cfg))
    assert loaded.db_path==path
    before=env.conn.total_changes
    assert cmd_asof(Namespace(config=str(cfg),as_of='2024-04-01',view='system',secucode=COMPANY,features=True))==0
    payload=json.loads(capsys.readouterr().out)
    assert payload['selected_count']==1 and len(payload['features'])==12
    assert env.conn.total_changes==before


def test_selected_observation_diagnostics_do_not_leak_from_later_observation(evidence_env):
    env=evidence_env
    first,_=observe(env,100,'2024-05-21T08:00:00Z',notice='2024-05-20')
    # Reuse the content version in a later-collected observation that explicitly
    # identifies an earlier disclosure. Public reconstruction may use that exact
    # declaration; its normalization metadata must not come from another observation.
    again,snapshot=observe(env,100,'2024-06-21T08:00:00Z',notice='2024-03-20')
    assert first==again
    env.conn.execute("UPDATE statement_observation SET normalization_diagnostics=? WHERE snapshot_id=?",
        (canonical_json([{'field':'total_operate_income','reason_code':'selected-observation-marker'}]),snapshot))
    env.conn.commit()
    report=query_asof(env.repo,'2024-04-01','public',include_features=True)
    row=report['statements'][0]
    assert row['selected_notice_date']=='2024-03-20'
    assert json.loads(row['normalization_diagnostics'])[0]['reason_code']=='selected-observation-marker'
    margin=next(r for r in report['features'] if r['feature_name']=='net_profit_margin')
    assert any(d['reason_code']=='selected-observation-marker' for d in margin['diagnostics'])
    assert margin['feature_value']==0.2


def test_asof_cli_missing_database_refuses_creation(tmp_path,capsys):
    from fin_feature.asof import cmd_asof
    path=tmp_path/'absent.db'
    cfg=tmp_path/'config.json';cfg.write_text(json.dumps({'database':{'path':str(path)}}))
    assert cmd_asof(Namespace(config=str(cfg),as_of='2024-01-01',view='system',secucode=None,features=False))==2
    assert not path.exists()
    assert 'As-of query failed' in capsys.readouterr().err


def test_asof_empty_company_is_reported_as_incomplete(evidence_env,tmp_path,capsys):
    from fin_feature.asof import cmd_asof
    env=evidence_env;observe(env,100,'2024-03-21T08:00:00Z')
    path=env.conn.execute('PRAGMA database_list').fetchone()[2]
    cfg=tmp_path/'config.json';cfg.write_text(json.dumps({'database':{'path':path}}))
    assert cmd_asof(Namespace(config=str(cfg),as_of='2024-04-01',view='system',secucode='999999.SH',features=False))==1
    assert json.loads(capsys.readouterr().out)['selected_count']==0
