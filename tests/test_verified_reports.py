"""Real issuer facts produce independent cross-period expectations and lineage."""
from decimal import Decimal
import json
import shutil

import pytest

from examples.verified_flow import ROOT, availability_for, load_reviewed, run_verified


@pytest.fixture(scope='module')
def verified(tmp_path_factory):
    root = tmp_path_factory.mktemp('verified issuers') / 'result'
    with pytest.MonkeyPatch.context() as patch:
        import requests
        patch.setattr(requests.sessions.Session, 'request', lambda *a, **k: pytest.fail('reviewed flow must be offline'))
        result = run_verified(root)
    return root, result


def test_reviewed_facts_keep_exact_units_and_physical_pdf_positions():
    fixtures = load_reviewed()
    assert len(fixtures) == 3
    assert sum(len(f['facts']) for f in fixtures.values()) == 78
    for fixture in fixtures.values():
        assert len({(f['report_date'], f['field']) for f in fixture['facts']}) == 26
        assert len(fixture['documents']) == 3
        for fact in fixture['facts']:
            assert fact['page'] > 0 and fact['row_label'] and fact['column_label']
            if 'document_value' in fact:
                printed = fact['document_value'].strip('()')
                factor = {'yuan': 1, 'thousand_yuan': 1000, 'ten_thousand_yuan': 10000, 'percent': 1}[fact['document_unit']]
                assert Decimal(printed) * factor == Decimal(fact['value'])


def test_three_real_companies_have_full_target_coverage_and_frozen_replay(verified):
    root, result = verified
    assert result['target_finite'] == result['target_positions'] == 36
    assert result['features'] == result['replay_count'] == result['replay_matches'] == 144
    assert result['non_null_features'] == 57
    assert result['evidence_audit_clean'] and result['network_requests'] == 0
    assert result['provider_records_approved'] is False
    assert all(row['not_computable_reason'] is None for row in result['target_features'])
    for name in ('midea', 'catl', 'moutai'):
        config = json.loads((root / name / 'config.json').read_bytes())['cleaning']['reviewed_inputs']
        assert all(e['evidence_ref'].startswith('examples/reviewed/' + name + '.json#') for e in config)
        assert all(e['alignment'] == name + '-2024-reviewed-reported-basis-v1' for e in config)


@pytest.mark.parametrize('code,expected', [
    ('000333.SZ', dict(current_ratio_change=-0.018201487971, debt_asset_ratio=0.623286683296,
        debt_asset_ratio_change=1.171225519132, equity_ratio=0.376713316704,
        gross_margin_feat=0.264189538686, net_profit_margin=0.094203664631,
        profit_qoq=-0.372357786708, profit_yoy=0.142862137783,
        revenue_qoq=-0.131993933792, revenue_yoy=0.094657570182,
        roe_change=-0.94, roe_change_rate=-0.042285200180)),
    ('300750.SZ', dict(current_ratio_change=-0.052255175312, debt_asset_ratio=0.652382444159,
        debt_asset_ratio_change=0.904481024897, equity_ratio=0.347617555841,
        gross_margin_feat=0.244448967369, net_profit_margin=0.140173818392,
        profit_qoq=0.122374484382, profit_yoy=0.150118917760,
        revenue_qoq=0.115844515993, revenue_yoy=-0.097038755237,
        roe_change=0.09, roe_change_rate=0.003743760399)),
])
def test_issuer_operands_have_independent_expected_feature_values(verified, code, expected):
    # Checked separately using report operands: FY-Q3 vs Q3-H1, total revenue
    # for growth, operating revenue for margin, not the provider's growth field.
    actual = {r['feature_name']: r['feature_value'] for r in verified[1]['target_features'] if r['secucode'] == code}
    assert actual == pytest.approx(expected, rel=1e-9, abs=1e-11)


def test_comparative_periods_inherit_actual_document_bound_not_report_year():
    for fixture in load_reviewed().values():
        bound = availability_for(fixture, {'2023-12-31'})
        assert bound['available_by'] == '2025-04-30'
        assert bound['first_public_at'] is None and bound['revision_public_at'] is None
        assert bound['historical_archive_verified'] is False
        assert len(bound['documents']) == 1 and bound['documents'][0]['id'] == 'annual'
        with pytest.raises(ValueError, match='requested periods'):
            availability_for(fixture, {'2022-12-31'})


@pytest.mark.parametrize('field', ['value', 'report_date', 'document', 'document_unit', 'page'])
def test_changed_facts_cannot_reuse_reviewed_approval(tmp_path, field):
    destination = tmp_path / 'facts'
    shutil.copytree(ROOT, destination)
    path = destination / 'catl.json'
    payload = json.loads(path.read_bytes())
    payload['facts'][0][field] = 'changed'
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match='review facts'):
        load_reviewed(destination)


def test_reviewed_approval_removal_restores_unit_and_comparability_gates(verified):
    from fin_feature.refresh import refresh_current
    from fin_feature.replay import replay_result
    from fin_feature.storage.database import get_connection
    from fin_feature.storage.raw_store import RawStore
    from fin_feature.storage.repository import Repository
    root, _ = verified
    conn = get_connection(str(root / 'catl/finance.db'))
    try:
        repo = Repository(conn, raw_store=RawStore(root / 'catl/raw', readonly=True))
        saved = conn.execute("SELECT result_id FROM feature_compute_log WHERE report_date='2024-12-31' AND feature_name='revenue_yoy'").fetchone()[0]
        refresh_current(repo)
        rejected = {r['feature_name']: r for r in conn.execute("SELECT * FROM features WHERE report_date='2024-12-31'")}
        assert rejected['revenue_yoy']['not_computable_reason'] == 'incomparable_basis'
        assert rejected['revenue_yoy']['feature_value'] is None
        assert rejected['current_ratio_change']['not_computable_reason'] == 'unknown_unit'
        assert replay_result(repo, saved)['match']
    finally:
        conn.close()
