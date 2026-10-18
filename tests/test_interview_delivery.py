"""Business coverage, conservative gates and executable interview delivery."""
import copy
import json
import subprocess
import sys

import pytest

from examples.interview_flow import run_interview_demo, load_facts
from fin_feature.cleaning.versioned import effective_rules, _prepare
from fin_feature.features.reviewed import comparison_bases
from fin_feature.features.engine import _check_period_comparability
from fin_feature.features.semantic import FieldSource
from fin_feature.demo_report import write_overview
from fin_feature.refresh import refresh_current
from fin_feature.replay import replay_result
from fin_feature.storage.database import get_connection
from fin_feature.storage.raw_store import RawStore
from fin_feature.storage.repository import Repository


@pytest.fixture
def interview(tmp_path, monkeypatch):
    import requests
    def forbidden(*args, **kwargs):
        raise AssertionError('offline interview demo must not open a network connection')
    monkeypatch.setattr(requests.Session, 'request', forbidden)
    root = tmp_path / 'interview'
    summary = run_interview_demo(root)
    conn = get_connection(str(root / 'finance.db'))
    yield root, Repository(conn, raw_store=RawStore(root / 'raw', readonly=True)), summary
    conn.close()


def test_required_features_have_independent_numerical_expectations_and_real_lineage(interview):
    root, repo, summary = interview
    # Independently checked against report operands, including FY-Q3 and Q3-H1.
    expected = dict(current_ratio_change=-1.7028329847873493, debt_asset_ratio=0.19044755657916054,
        debt_asset_ratio_change=5.41625191233453, equity_ratio=0.8095524434208394,
        gross_margin_feat=0.9193121663606457, net_profit_margin=0.49515407812791257,
        profit_qoq=0.327653797578137, profit_yoy=0.1537996610162563,
        revenue_qoq=0.28610423766754667, revenue_yoy=0.1566397974302482,
        roe_change=1.83, roe_change_rate=0.053524422345715236)
    actual = {r['feature_name']: r['feature_value'] for r in summary['target_features']}
    assert actual == pytest.approx(expected)
    assert summary['demonstrated_features'] == summary['required_features'] == 12
    assert summary['features'] == summary['replay_count'] == summary['replay_matches'] == 48
    assert summary['non_null_features'] == 19  # Other periods still have honest NULLs.
    assert summary['evidence_audit_clean'] and summary['report_exit_code'] == 0
    assert summary['network_requests'] == 0 and not summary['original_vendor_responses']
    assert repo.conn.execute('SELECT COUNT(*) FROM statement_version WHERE evidence_sufficient=1').fetchone()[0] == 9
    inputs = repo.conn.execute("SELECT config_snapshot FROM feature_compute_log WHERE feature_name='revenue_qoq' AND report_date='2024-12-31'").fetchone()[0]
    snapshot = json.loads(inputs)
    assert {i['report_date'] for i in snapshot['inputs']} == {'2024-12-31', '2024-09-30', '2024-06-30'}
    assert snapshot['cleaning']['reviewed_inputs']
    assert len(list((root / 'raw').rglob('*.body.gz'))) == 3
    assert {c['reason'] for c in summary['boundary_cases']} == {'missing_input', 'non_positive_comparison_base', 'unknown_unit'}


def test_removing_review_restores_default_rejection_and_saved_replay_still_works(interview):
    _, repo, _ = interview
    saved = repo.conn.execute("SELECT result_id FROM feature_compute_log WHERE feature_name='revenue_yoy' AND report_date='2024-12-31'").fetchone()[0]
    refresh_current(repo)
    rows = {r['feature_name']: r for r in repo.conn.execute("SELECT * FROM features WHERE report_date='2024-12-31'")}
    assert rows['revenue_yoy']['feature_value'] is None
    assert rows['revenue_yoy']['not_computable_reason'] == 'incomparable_basis'
    assert rows['current_ratio_change']['feature_value'] is None
    assert rows['current_ratio_change']['not_computable_reason'] == 'unknown_unit'
    replay = replay_result(repo, saved)
    assert replay['match'] and replay['replayed_value'] == pytest.approx(0.1566397974302482)


def test_saved_review_applies_to_system_asof_but_does_not_invent_public_dates(interview):
    from datetime import datetime, timezone, timedelta
    from fin_feature.asof import query_asof
    root, repo, _ = interview
    cleaning = json.loads((root / 'config.json').read_text())['cleaning']
    when = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
    result = query_asof(repo, when, config=cleaning, include_features=True)
    target = [r for r in result['features'] if r['report_date'] == '2024-12-31']
    assert len(target) == 12 and all(r['feature_value'] is not None for r in target)
    public = query_asof(repo, when[:10], view='public', config=cleaning)
    assert public['selected_count'] == 0


@pytest.mark.parametrize('change', ['raw', 'metric', 'semantic'])
def test_review_cannot_leak_to_changed_content(interview, change):
    root, repo, _ = interview
    config = effective_rules(json.loads((root / 'config.json').read_text())['cleaning'])
    version = dict(repo.conn.execute("SELECT * FROM statement_version WHERE table_type='balance_sheet' LIMIT 1").fetchone())
    if change == 'raw':
        version['raw_fields'] = '{}'
    else:
        version[change + '_hash'] = '0' * 64
    with pytest.raises(ValueError, match='new review'):
        _prepare(version, config)


@pytest.mark.parametrize('change', ['alignment', 'currency', 'scope', 'amount_unit', 'period_basis', 'definition'])
def test_reviewed_cross_period_comparison_requires_identical_field_semantics(interview, change):
    root, repo, _ = interview
    config = effective_rules(json.loads((root / 'config.json').read_text())['cleaning'])
    version = dict(repo.conn.execute("SELECT * FROM statement_version WHERE table_type='income_statement' LIMIT 1").fetchone())
    before = comparison_bases(version, config)['total_operate_income']
    changed = copy.deepcopy(config)
    entry = next(e for e in changed['reviewed_inputs'] if e['table_type'] == version['table_type'] and e['report_date'] == version['report_date'])
    if change == 'definition':
        entry['fields']['total_operate_income']['definition'] = 'other revenue definition'
    else:
        entry[change] = 'different'
    after = comparison_bases(version, changed)['total_operate_income']
    current = {'total_operate_income': 120, '__sources__': {'total_operate_income': FieldSource('income_statement', comparison_basis=before)}}
    previous = {'total_operate_income': 100, '__sources__': {'total_operate_income': FieldSource('income_statement', comparison_basis=after)}}
    diagnostics = _check_period_comparability(current, previous, 'previous')
    assert previous['total_operate_income'] is None
    assert diagnostics[0]['reason_code'] == 'incomparable_basis'


def test_config_rejects_missing_evidence_and_incompatible_units(interview):
    root, _, _ = interview
    cleaning = json.loads((root / 'config.json').read_text())['cleaning']
    bad = copy.deepcopy(cleaning)
    bad['reviewed_inputs'][0]['evidence_ref'] = ''
    with pytest.raises(ValueError, match='evidence reference'):
        effective_rules(bad)
    bad = copy.deepcopy(cleaning)
    field = next(iter(bad['reviewed_inputs'][0]['fields']))
    bad['reviewed_inputs'][0]['fields'][field]['unit'] = 'times'
    with pytest.raises(ValueError, match='conflicts'):
        effective_rules(bad)


def test_fixture_changes_require_re_review(tmp_path):
    from examples.interview_flow import FIXTURE
    changed = tmp_path / 'changed.json'
    fixture = json.loads(FIXTURE.read_bytes())
    fixture['facts'][0]['value'] = '999999'
    changed.write_text(json.dumps(fixture))
    with pytest.raises(ValueError, match='review facts'):
        load_facts(changed)


@pytest.mark.parametrize('newline', ['\n', '\r\n'])
def test_reviewed_fixture_survives_git_line_endings_and_json_formatting(tmp_path, newline):
    from examples.interview_flow import FIXTURE
    fixture = json.loads(FIXTURE.read_bytes())
    changed = tmp_path / 'checkout.json'
    changed.write_bytes(json.dumps(fixture, ensure_ascii=False, indent=4, sort_keys=True).replace('\n', newline).encode())
    assert load_facts(changed) == fixture


def test_overview_contains_actual_results_and_no_remote_assets(interview, tmp_path):
    _, _, summary = interview
    target = write_overview(tmp_path, {'steps': [{'name': 'interview', 'exit_code': 0, 'summary': summary}]})
    body = target.read_text()
    assert '15.66%' in body and '1.8300 百分点' in body and '-1.7028 倍数差' in body
    assert '19' in body and '48/48' in body and '人工输入' in body
    assert '<script' not in body and 'src="http' not in body


def test_demo_cli_propagates_failure_and_generates_error_overview(tmp_path):
    # Existing output paths must be rejected without overwriting user files.
    output = tmp_path / 'output'
    output.mkdir()
    keep = output / 'keep.txt'; keep.write_text('keep')
    result = subprocess.run([sys.executable, 'scripts/demo.py', '--output-dir', str(output)], capture_output=True)
    assert result.returncode != 0 and keep.read_text() == 'keep'
