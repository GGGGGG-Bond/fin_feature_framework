"""Captured content identity, complete denominators and actual offline end-to-end flow."""
from collections import Counter
from decimal import Decimal
import json
import shutil

import pytest
from examples.showcase_flow import load_snapshots, dual_comparison, run_showcase, SNAPSHOTS, official_balance_pages


def test_real_snapshot_fixed_coverage_and_official_checks():
    data=load_snapshots();primary=data['financials.json']
    assert len(primary['companies'])==8 and len(primary['pages'])==24
    assert all(len(p['rows'])==8 and len(p['raw_sha256'])==64 for p in primary['pages'])
    rows=dual_comparison(data['dual-source.json'],data['official-reviews.json'])
    assert len(rows)==96
    assert Counter(r['official_status'] for r in rows)=={'matches_report':81,'differs_from_report':3,'missing_or_definition':12}
    differences=[r for r in rows if r['official_status']=='differs_from_report']
    assert all(r['secucode']=='600031.SH' and r['matches_later_comparative'] for r in differences)
    profit=next(r for r in differences if r['field']=='parent_netprofit')
    assert profit['official_value']=='5975451000' and Decimal(profit['eastmoney'])==Decimal('5955567000')
    assert all(not r['upstream_independence_verified'] for r in rows)


def test_snapshot_identity_survives_crlf_but_rejects_financial_tamper(tmp_path):
    shutil.copytree(SNAPSHOTS,tmp_path/'snapshots')
    root=tmp_path/'snapshots';path=root/'financials.json'
    path.write_bytes(path.read_bytes().replace(b'\r\n',b'\n').replace(b'\n',b'\r\n'))
    load_snapshots(root)
    doc=json.loads(path.read_bytes());doc['pages'][0]['rows'][0]['PARENT_NETPROFIT']=1
    path.write_text(json.dumps(doc))
    with pytest.raises(ValueError,match='identity'):load_snapshots(root)


def test_report_fact_units_and_cost_parentheses():
    reviews=load_snapshots()['official-reviews.json']
    for r in reviews['facts']:
        unit=r.get('document_unit','yuan');printed=r.get('document_value',r['value'])
        if r.get('conversion')=='expense_magnitude':printed=printed.strip('()')
        assert Decimal(printed)*{'yuan':1,'thousand_yuan':1000,'million_yuan':1000000}[unit]==Decimal(r['value'])
    assert len(official_balance_pages(reviews))==8


def test_showcase_is_offline_and_runs_real_pipelines(tmp_path,monkeypatch):
    import requests
    monkeypatch.setattr(requests.sessions.Session,'request',lambda *a,**kw:pytest.fail('offline showcase attempted network'))
    output=tmp_path/'showcase'
    summary=run_showcase(output)
    assert summary['features']==768 and summary['replay_matches']==768
    assert summary['replay_count']==768 and summary['evidence_audit_clean']
    assert summary['required_features_demonstrated']==12
    assert summary['backtest_members']==8 and summary['backtest_groups']==5
    assert summary['historical_current_provider_rejected']==8
    assert summary['network_requests']==0
    assert summary['reviewed_target_finite'] == summary['reviewed_target_positions'] == 36
    assert summary['reviewed_replay_matches'] == summary['reviewed_replay_count'] == 144
    assert summary['reviewed_facts'] == 78 and summary['reviewed_companies'] == 3
    assert summary['return_study_scenarios'] == 9 and summary['return_study_status'] == 'success'
    backtest=json.loads((output/'backtest/summary.json').read_bytes())
    assert sorted(r['size'] for r in backtest['groups'])==[1,1,2,2,2]
    assert backtest['entry_date']=='2025-05-06' and backtest['exit_date']=='2025-12-31'
    signals=json.loads((output/'signals.json').read_bytes())
    sany=next(r for r in signals['records'] if r['secucode']=='600031.SH')
    assert sany['feature_value']==pytest.approx(79143477000/152145076000)
    editions = json.loads((output/'disclosure-editions.json').read_bytes())
    from fin_feature.addons.return_study import select_public_signals
    candidates = dict(schema='return-signals-v1', feature_name='debt_asset_ratio', records=[
        dict(secucode=editions['secucode'], report_date=editions['report_date'], available_at=e['available_by'],
             feature_value=float(Decimal(e['values']['total_liabilities'])/Decimal(e['values']['total_assets'])),
             evidence_ref=e['document']['url']) for e in editions['editions']])
    historical, missing = select_public_signals(candidates, '2025-05-05', ['600031.SH'])
    assert not missing and historical['records'][0]['feature_value'] == sany['feature_value']
    later, _ = select_public_signals(candidates, '2026-04-01', ['600031.SH'])
    assert later['records'][0]['feature_value'] != sany['feature_value']
    assert editions['first_public_at'] is None and editions['exact_revision_public_at'] is None
    html=(output/'index.html').read_text()
    assert 'dual-table' in html and 'features-table' in html and '<svg' in html
    assert 'reviewed-table' in html and 'return-study/sensitivity.csv' in html
    assert 'cdn.' not in html and 'src="https://' not in html
    from scripts.export_results import export_tree
    portable=export_tree(output,tmp_path/'portable')
    assert (portable/'captured/report/inputs.json').is_file()
    assert not list(portable.rglob('*.db')) and not list(portable.rglob('*.gz'))
