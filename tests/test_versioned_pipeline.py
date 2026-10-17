"""C01-C15: real temporary SQLite, immutable versions, evidence and failure injection."""
from argparse import Namespace
import json
import sqlite3

import pytest

from fin_feature.cleaning.pipeline import run_cleaning
from fin_feature.cleaning.versioned import VersionedCleanRepository, effective_rules
from fin_feature.features.engine import _build_context, compute_all
from fin_feature.features.standardize import FIELD_UNITS, clear_unit_overrides, set_unit_override, standardize_record
from fin_feature.storage.database import get_connection, init_schema
from fin_feature.storage.hashing import canonical_json
from fin_feature.storage import migrations as migration
from fin_feature.storage.repository import Repository


COMPANY = '000001.SZ'
DATE = '2025-03-31'
DEFAULTS = {
    'financial_indicator': {'total_operate_income': 100, 'parent_netprofit': 20,
                            'weightavg_roe': 16.75, 'gross_margin': 40},
    'income_statement': {'total_operate_income': 100, 'parent_netprofit': 20},
    'balance_sheet': {'total_assets': 1000, 'total_liabilities': 600, 'total_equity': 400, 'current_ratio': 200},
}


def seed(repo, table='financial_indicator', date=DATE, company=COMPANY, **updates):
    raw = {**DEFAULTS[table], **updates, 'secucode': company, 'report_date': date}
    numeric = {name: value for name, value in raw.items() if name not in ('secucode', 'report_date', 'industry_name')}
    values, diagnostics = standardize_record(numeric, unit_map=FIELD_UNITS)
    with repo.unit_of_work():
        return repo.create_statement_version(company, date, table,
            standardized_fields=canonical_json(values), raw_fields=canonical_json(raw),
            normalization_diagnostics=canonical_json(diagnostics))


def clean(repo, config=None):
    return run_cleaning(repo, config=config, input_mode='versions')


def compute(repo, groups=None, config=None):
    return compute_all(repo, groups, input_mode='versions', config=config)


def rows(repo, table):
    return [dict(row) for row in repo.conn.execute(f'SELECT * FROM {table} ORDER BY rowid')]


def latest(repo, feature, date=DATE):
    return repo.conn.execute('SELECT * FROM features WHERE feature_name=? AND report_date=? AND secucode=?', (feature, date, COMPANY)).fetchone()


class CommitFails:
    """Delegate all real SQLite operations except the actual commit boundary."""
    def __init__(self, conn):
        self.conn = conn

    def __getattr__(self, name):
        return getattr(self.conn, name)

    def commit(self):
        raise sqlite3.OperationalError('injected actual commit failure')


def test_c01_v4_upgrade_backup_preserves_rows_without_inventing_refs(tmp_path):
    path = tmp_path/'legacy.db'
    conn = get_connection(str(path))
    for version, name, fn in migration.MIGRATIONS:
        if version > 4:
            break
        migration._ensure_schema_version_table(conn)
        migration._apply_migration(conn, version, name, fn)
    conn.execute("INSERT INTO clean_financial_indicator(secucode,report_date,weightavg_roe) VALUES ('000001.SZ','2025-03-31',16.75)")
    conn.commit(); conn.close()
    init_schema(str(path))
    conn = get_connection(str(path))
    assert conn.execute('SELECT weightavg_roe,record_ref,clean_ref FROM clean_financial_indicator').fetchone()[:] == (16.75,None,None)
    assert conn.execute('SELECT COUNT(*) FROM clean_statement').fetchone()[0] == 0
    assert migration._get_current_version(conn) == 5
    backups = list(tmp_path.glob('*.backup-*'))
    assert len(backups) == 1
    copy = sqlite3.connect(backups[0])
    assert migration._get_current_version(copy) == 4
    assert copy.execute('SELECT weightavg_roe FROM clean_financial_indicator').fetchone()[0] == 16.75
    copy.close(); conn.close()
    init_schema(str(path))
    assert list(tmp_path.glob('*.backup-*')) == backups


def test_c01_v5_incomplete_structure_is_rejected(repo):
    repo.conn.execute('ALTER TABLE clean_statement RENAME COLUMN diagnostics TO missing_diagnostics')
    repo.conn.commit()
    with pytest.raises(RuntimeError, match='v5'):
        migration.run_migrations(repo.conn)


def test_c01_v5_constraint_impostor_is_rejected(repo):
    repo.conn.execute('DROP TABLE clean_statement')
    repo.conn.execute('CREATE TABLE clean_statement(clean_id TEXT PRIMARY KEY,record_ref TEXT,config_hash TEXT,config_snapshot TEXT,status TEXT,standardized_fields TEXT,compatibility_fields TEXT,diagnostics TEXT)')
    repo.commit()
    with pytest.raises(RuntimeError, match='unique.*foreign keys.*status constraint'):
        migration.run_migrations(repo.conn)


def test_c02_v5_failure_rollback_preserves_outer_transaction(tmp_path):
    conn = get_connection(str(tmp_path/'rollback.db'))
    for version, name, fn in migration.MIGRATIONS:
        if version > 4:
            break
        migration._ensure_schema_version_table(conn)
        migration._apply_migration(conn, version, name, fn)
    conn.execute("INSERT INTO stock_list(secucode,security_code,security_name,market) VALUES ('000001.SZ','000001','fixture','SZ')")
    def fail(c):
        migration.MIGRATIONS[-1][2](c)
        raise OSError('after v5 DDL')
    with pytest.raises(RuntimeError, match='after v5 DDL'):
        migration._apply_migration(conn, 5, 'v5', fail)
    assert conn.in_transaction
    assert conn.execute('SELECT COUNT(*) FROM stock_list').fetchone()[0] == 1
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='clean_statement'").fetchone() is None
    assert 'record_ref' not in {r[1] for r in conn.execute('PRAGMA table_info(clean_financial_indicator)')}
    conn.rollback(); conn.close()


def test_c03_versions_are_authoritative_and_no_raw_fallback(repo):
    with repo.unit_of_work():
        repo.batch_upsert_raw('financial_indicator', [{'secucode': COMPANY, 'report_date': DATE, 'weightavg_roe': 999}])
    assert all(stat.clean_count == 0 for stat in clean(repo))
    assert compute(repo) == 0
    version = seed(repo)
    clean(repo)
    assert repo.get_clean_record('financial_indicator', COMPANY, DATE)['weightavg_roe'] == 16.75
    assert repo.get_clean_record('financial_indicator', COMPANY, DATE)['record_ref'] == version


def test_c04_c05_standard_values_convert_once_and_carry_refs(repo):
    version = seed(repo)
    seed(repo, 'income_statement')
    seed(repo, 'balance_sheet')
    set_unit_override('weightavg_roe', 'amount')
    set_unit_override('current_ratio', 'times')
    try:
        clean(repo)
        adapter = VersionedCleanRepository(repo)
        ctx = _build_context(adapter, COMPANY, DATE)
        assert ctx.current['weightavg_roe'] == 0.1675
        assert ctx.current['gross_margin'] == 0.4
        assert ctx.current['current_ratio'] is None
        assert ctx.current['__sources__']['weightavg_roe'].record_ref == version
        assert compute(repo) == 12
        assert latest(repo, 'gross_margin_feat')['feature_value'] == 0.4
        assert latest(repo, 'debt_asset_ratio')['feature_value'] == 0.6
        compat = repo.get_clean_record('financial_indicator', COMPANY, DATE)
        assert compat['weightavg_roe'] == 16.75
        result = repo.conn.execute('SELECT * FROM clean_statement WHERE clean_id=?', (compat['clean_ref'],)).fetchone()
        assert json.loads(result['standardized_fields'])['weightavg_roe'] == 0.1675
        assert result['record_ref'] == version
    finally:
        clear_unit_overrides()


def test_c06_idempotent_cleaning_and_config_change(repo):
    seed(repo)
    clean(repo); compute(repo)
    before = rows(repo, 'features')
    clean(repo)
    assert rows(repo, 'features') == before
    assert len(rows(repo, 'clean_statement')) == 1
    cfg = {'outlier_thresholds': {'gross_margin': [0, 0.2]}}
    clean(repo, cfg)
    assert len(rows(repo, 'clean_statement')) == 2
    assert rows(repo, 'features') == []
    assert any(d['reason_code'] == 'outlier_threshold' for d in json.loads(rows(repo, 'clean_statement')[-1]['diagnostics']))
    compute(repo, config=cfg)
    assert latest(repo, 'gross_margin_feat')['feature_value'] == 0.4


@pytest.mark.parametrize('raw_value', [200,None])
def test_c06_zero_cannot_manufacture_unknown_unit(repo, raw_value):
    seed(repo, 'balance_sheet', current_ratio=raw_value)
    cfg = {'missing_strategy': {'current_ratio': 'zero'}}
    clean(repo, cfg); compute(repo, config=cfg)
    result = latest(repo, 'current_ratio_change')
    assert result['feature_value'] is None
    assert result['not_computable_reason'] == 'unknown_unit'


def test_c07_drop_invalidates_comparison_period_projections_but_preserves_logs(repo):
    seed(repo, 'income_statement', '2024-03-31')
    seed(repo, 'income_statement')
    seed(repo, company='000002.SZ')
    clean(repo); compute(repo)
    history = rows(repo, 'feature_compute_log')
    with repo.unit_of_work():
        repo.upsert_feature(COMPANY, DATE, 'legacy_fixture', 7, 'fixture', None)
        repo.upsert_feature(COMPANY, DATE, 'other_fixture', 8, 'fixture', 'stage9')
    seed(repo, 'income_statement', '2024-03-31', parent_netprofit=None)
    clean(repo)
    assert repo.get_clean_record('income_statement', COMPANY, '2024-03-31') is None
    assert latest(repo, 'revenue_yoy') is None
    assert latest(repo, 'legacy_fixture')['feature_value'] == 7
    assert latest(repo, 'other_fixture')['feature_value'] == 8
    assert repo.conn.execute("SELECT COUNT(*) FROM features WHERE secucode='000002.SZ'").fetchone()[0] == 12
    assert rows(repo, 'feature_compute_log') == history
    compute(repo)
    assert latest(repo, 'revenue_yoy')['feature_value'] is None
    # A new dropped revision must invalidate a previously computed None result as well.
    seed(repo, 'income_statement', '2024-03-31', total_operate_income=200, parent_netprofit=None)
    clean(repo)
    assert latest(repo, 'revenue_yoy') is None


@pytest.mark.parametrize('boundary', ['projection', 'commit'])
def test_c08_clean_failure_is_atomic_after_reconnect(repo, db, monkeypatch, boundary):
    seed(repo); clean(repo); compute(repo)
    tables = ['clean_statement', 'clean_selection', 'clean_financial_indicator', 'features', 'clean_log']
    before = {t: rows(repo, t) for t in tables}
    seed(repo, weightavg_roe=20)
    if boundary == 'projection':
        original = repo.upsert_clean
        def fail(*args):
            original(*args)
            raise OSError('after current projection')
        monkeypatch.setattr(repo, 'upsert_clean', fail)
    else:
        monkeypatch.setattr(repo, 'conn', CommitFails(repo.conn))
    with pytest.raises((OSError, sqlite3.OperationalError)):
        clean(repo)
    fresh = get_connection(db.db_path)
    assert {t: rows(Repository(fresh), t) for t in tables} == before
    fresh.close()


@pytest.mark.parametrize('date,expected', [
    ('2025-03-31', {'2025-03-31','2024-12-31','2024-09-30'}),
    ('2025-06-30', {'2025-06-30','2025-03-31'}),
    ('2025-09-30', {'2025-09-30','2025-06-30','2025-03-31'}),
])
def test_c09_qoq_lineage_includes_only_required_differencing_periods(repo, date, expected):
    dates = expected | {'2023-03-31'}
    versions = {period: seed(repo, 'income_statement', period) for period in dates}
    clean(repo); compute(repo, ['growth'])
    row = latest(repo, 'revenue_qoq', date)
    assert set(json.loads(row['input_version_ids'])) == {versions[p] for p in expected}
    assert row['feature_value'] is None
    assert row['not_computable_reason'] == 'incomparable_basis'


def test_c09_yoy_and_current_lineage_exclude_unrelated_periods(repo):
    current = seed(repo)
    year = seed(repo, date='2024-03-31')
    seed(repo, date='2024-12-31')
    clean(repo); compute(repo)
    assert set(json.loads(latest(repo, 'roe_change')['input_version_ids'])) == {current, year}
    gross_inputs = json.loads(latest(repo, 'gross_margin_feat')['input_version_ids'])
    assert gross_inputs == [current]


def test_c10_unknown_basis_remains_blocking_and_none_has_reason(repo):
    seed(repo); seed(repo, date='2024-03-31')
    seed(repo, 'balance_sheet'); seed(repo, 'balance_sheet', '2024-12-31')
    clean(repo); compute(repo)
    assert latest(repo, 'roe_change')['not_computable_reason'] == 'incomparable_basis'
    assert latest(repo, 'debt_asset_ratio_change')['feature_value'] is None
    assert latest(repo, 'debt_asset_ratio')['feature_value'] == 0.6
    for result in rows(repo, 'feature_compute_log'):
        assert (result['feature_value'] is None) == (result['not_computable_reason'] is not None)
        assert result['as_of_date'] is None


def test_c10_nonpositive_and_industry_diagnostics(repo):
    seed(repo, gross_margin=40)
    seed(repo, 'income_statement', total_operate_income=0, industry_name='银行')
    clean(repo); compute(repo)
    assert latest(repo, 'net_profit_margin')['not_computable_reason'] == 'non_positive_base'
    assert latest(repo, 'gross_margin_feat')['not_computable_reason'] == 'not_applicable'


def test_c11_repeated_computation_appends_logs_and_updates_projection(repo):
    seed(repo); clean(repo); compute(repo)
    first = rows(repo, 'feature_compute_log')
    compute(repo)
    results = rows(repo, 'feature_compute_log')
    assert len(results) == 24 and results[:12] == first
    assert len({r['compute_run_id'] for r in results}) == 2
    for projection in rows(repo, 'features'):
        logs = [r for r in results if r['feature_name'] == projection['feature_name']]
        assert projection['compute_run_id'] == logs[-1]['compute_run_id']
        assert projection['input_version_ids'] == logs[-1]['input_version_ids']
        snapshot = json.loads(logs[-1]['config_snapshot'])
        assert snapshot['code_identity']['commit'] == projection['code_commit']


@pytest.mark.parametrize('cause', ['revision', 'rules', 'identity', 'sidecar', 'content'])
def test_c12_stale_or_corrupt_inputs_fail_before_any_output(repo, cause):
    seed(repo); clean(repo); compute(repo)
    before = rows(repo, 'feature_compute_log')
    cfg = None
    if cause == 'revision':
        seed(repo, weightavg_roe=20)
    elif cause == 'rules':
        cfg = {'outlier_thresholds': {'gross_margin':[0,0.5]}}
    elif cause == 'identity':
        repo.conn.execute('UPDATE clean_financial_indicator SET record_ref=NULL'); repo.commit()
    elif cause == 'sidecar':
        repo.conn.execute("UPDATE clean_statement SET standardized_fields='{}'"); repo.commit()
    else:
        repo.conn.execute("UPDATE statement_version SET standardized_fields='{}'"); repo.commit()
    with pytest.raises(ValueError):
        compute(repo, config=cfg)
    assert rows(repo, 'feature_compute_log') == before


@pytest.mark.parametrize('invalid_date', ['2025-03-01', '2025/03/31'])
def test_c12_report_period_must_be_normalized_quarter_end(repo, invalid_date):
    seed(repo, date=invalid_date)
    with pytest.raises(ValueError, match='quarter end'):
        clean(repo)
    assert rows(repo,'clean_statement') == []
    assert rows(repo,'clean_selection') == []


@pytest.mark.parametrize('boundary', ['log', 'projection', 'commit'])
def test_c13_compute_failures_are_atomic_after_reconnect(repo, db, monkeypatch, boundary):
    seed(repo); clean(repo); compute(repo)
    before = {table: rows(repo,table) for table in ('features','feature_compute_log')}
    method = {'log': 'save_compute_log', 'projection': 'upsert_versioned_feature', 'commit': 'commit'}[boundary]
    original = getattr(repo, method)
    def fail(*args, **kwargs):
        if boundary != 'commit':
            original(*args, **kwargs)
        raise sqlite3.OperationalError('injected actual write/commit failure')
    if boundary == 'commit':
        monkeypatch.setattr(repo, 'conn', CommitFails(repo.conn))
    else:
        monkeypatch.setattr(repo,method,fail)
    with pytest.raises(sqlite3.OperationalError):
        compute(repo)
    fresh = get_connection(db.db_path)
    assert {table: rows(Repository(fresh),table) for table in before} == before
    fresh.close()


def test_c13_legacy_projection_does_not_block_immutable_new_log(repo):
    seed(repo); clean(repo)
    with repo.unit_of_work():
        repo.upsert_feature(COMPANY,DATE,'gross_margin_feat',99,'profitability',None)
    assert compute(repo) == 11
    assert latest(repo,'gross_margin_feat')['feature_value'] == 99
    assert len(rows(repo,'feature_compute_log')) == 12
    logged = [r for r in rows(repo,'feature_compute_log') if r['feature_name']=='gross_margin_feat'][0]
    assert logged['feature_value'] == 0.4


def test_c15_cli_feature_trace_reaches_real_http_body(tmp_path, monkeypatch):
    from test_ingestion_cli import configure, invoke, Session, query
    from fin_feature import cli
    from fin_feature.storage.raw_store import RawStore
    cfg,path = configure(tmp_path)
    assert invoke(monkeypatch,cfg,Session(path),'crawl') == 0
    cli.cmd_clean(Namespace(config=str(cfg)))
    cli.cmd_features(Namespace(config=str(cfg)))
    logs = query(path, 'SELECT * FROM feature_compute_log')
    assert len(logs) == 24
    store = RawStore(tmp_path/'evidence', readonly=True)
    observed = 0
    for result in logs:
        for version in json.loads(result['input_version_ids']):
            located = query(path, 'SELECT o.row_index,s.* FROM statement_observation o JOIN raw_snapshot s ON s.snapshot_id=o.snapshot_id WHERE o.version_id=?', (version,))
            assert located
            for snapshot in located:
                body = store.verify({'file_path':snapshot['file_path'], 'raw_hash':snapshot['raw_content_hash'], 'raw_bytes':snapshot['raw_bytes']}).body
                assert json.loads(body)['result']['data'][snapshot['row_index']]['SECUCODE'] == result['secucode']
                observed += 1
    assert observed > 0
    assert {r['evidence_sufficient'] for r in query(path,'SELECT evidence_sufficient FROM statement_version')} == {1}


def test_c15_legacy_input_stays_unverified_after_compute(repo):
    seed(repo); clean(repo); compute(repo)
    assert rows(repo,'statement_version')[0]['evidence_sufficient'] == 0
    result = rows(repo,'feature_compute_log')[0]
    assert all(i['evidence_sufficient'] == 0 for i in json.loads(result['config_snapshot'])['inputs'])


def test_c09_c10_explicit_comparable_fixture_retains_numeric_formulas(repo, monkeypatch):
    from fin_feature.features import engine
    original = engine._check_period_comparability
    def explicitly_comparable(current, comparison, label):
        for record in (current, comparison):
            if record:
                for source in record.get('__sources__', {}).values():
                    source.comparable = True
        return original(current, comparison, label)
    monkeypatch.setattr(engine, '_check_period_comparability', explicitly_comparable)
    seed(repo, weightavg_roe=20)
    seed(repo, date='2024-03-31', weightavg_roe=10)
    seed(repo, 'income_statement', total_operate_income=300)
    seed(repo, 'income_statement', '2024-12-31', total_operate_income=450)
    seed(repo, 'income_statement', '2024-09-30', total_operate_income=200)
    seed(repo, 'income_statement', '2024-03-31', total_operate_income=250, parent_netprofit=-10)
    clean(repo); compute(repo)
    assert latest(repo,'roe_change')['feature_value'] == 10
    assert latest(repo,'roe_change_rate')['feature_value'] == 1
    assert latest(repo,'revenue_qoq')['feature_value'] == pytest.approx(0.2)
    assert latest(repo,'revenue_yoy')['feature_value'] == pytest.approx(0.2)
    assert latest(repo,'profit_yoy')['feature_value'] is None
    assert latest(repo,'profit_yoy')['not_computable_reason'] == 'non_positive_comparison_base'


@pytest.mark.parametrize('outcome', ['exception','infinite'])
def test_c10_calculation_failures_have_explicit_reason(repo, monkeypatch, outcome):
    from fin_feature.features.profitability import GrossMarginFeature
    seed(repo); clean(repo)
    def fail(self, ctx):
        if outcome == 'exception':
            raise ArithmeticError('calculation fixture failure')
        return float('inf')
    monkeypatch.setattr(GrossMarginFeature, 'calculate', fail)
    compute(repo)
    result = latest(repo,'gross_margin_feat')
    assert result['feature_value'] is None
    assert result['not_computable_reason'] == ('calculation_error' if outcome == 'exception' else 'invalid_value')


def test_c13_other_definition_with_same_key_is_preserved(repo):
    seed(repo); clean(repo)
    with repo.unit_of_work():
        repo.upsert_feature(COMPANY,DATE,'gross_margin_feat',99,'profitability','stage9')
    assert compute(repo) == 11
    assert latest(repo,'gross_margin_feat')['feature_value'] == 99
    assert latest(repo,'gross_margin_feat')['definition_version'] == 'stage9'
    assert len(rows(repo,'feature_compute_log')) == 12


@pytest.mark.parametrize('operation', ['clean','compute'])
def test_c08_c13_outer_transaction_is_not_committed_by_version_work(repo, db, operation):
    seed(repo); clean(repo)
    before = {table: rows(repo,table) for table in ('clean_statement','features','feature_compute_log')}
    repo.conn.execute('BEGIN')
    if operation == 'compute':
        compute(repo)
        assert len(rows(repo,'feature_compute_log')) == 12
    else:
        clean(repo, {'outlier_thresholds': {'gross_margin':[0,0.2]}})
        assert len(rows(repo,'clean_statement')) == 2
    assert repo.conn.in_transaction
    repo.rollback()
    fresh = get_connection(db.db_path)
    assert {table: rows(Repository(fresh),table) for table in before} == before
    fresh.close()


@pytest.mark.parametrize('config', [[], {'missing_strategy':[]}, {'outlier_thresholds':[]},
    {'missing_strategy':{'parent_netprofit':'impute'}}, {'outlier_thresholds':{'gross_margin':[1,0]}}])
def test_c14_invalid_cleaning_config_is_rejected_without_writes(repo, config):
    seed(repo)
    with pytest.raises((ValueError, TypeError)):
        clean(repo,config)
    assert not rows(repo,'clean_statement')


@pytest.mark.parametrize('command', ['cmd_clean', 'cmd_features'])
def test_c14_invalid_cli_rules_are_rejected_before_db_initialization(tmp_path, monkeypatch, command):
    import yaml
    from fin_feature import cli
    path = tmp_path/'invalid.yaml'
    path.write_text(yaml.safe_dump({'cleaning': {'missing_strategy': ['invalid']}}))
    def forbidden(*args):
        raise AssertionError('database initialization must not happen')
    monkeypatch.setattr(cli, '_get_db_and_repo', forbidden)
    with pytest.raises(ValueError):
        getattr(cli, command)(Namespace(config=str(path)))
