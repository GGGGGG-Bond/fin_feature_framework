"""Financial behavior, time gates and failure denominators, not implementation mirrors."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest
from fin_feature.addons.returns import run_return_backtest, cmd_return_backtest


@pytest.fixture
def inputs():
    codes = ['000001.SZ','000002.SZ','000003.SZ','000004.SZ']
    signals = dict(schema='return-signals-v1', feature_name='test_factor', records=[
        dict(secucode=code, report_date='2023-12-31', feature_value=i, available_at='2024-04-30', evidence_ref='reviewed-report')
        for i,code in enumerate(codes)])
    prices = dict(schema='captured-adjusted-prices-v1', instruments=[dict(secucode=code, price_basis='provider_qfq', bars=[
        dict(date='2024-05-06',open=100,close=90),dict(date='2024-05-07',open=95,close=120),
        dict(date='2024-05-08',open=120,close=150 if i%2==0 else 50)]) for i,code in enumerate(codes+['510300.SH'])])
    return signals, prices


def run(inputs, **kwargs):
    return run_return_backtest(*inputs, formation_date='2024-05-01', end_date='2024-05-08', n_groups=2, **kwargs)


def test_real_prices_next_session_equal_initial_capital_and_two_sided_cost(inputs):
    result = run(inputs,cost_bps=10)
    assert result['entry_date']=='2024-05-06'
    assert result['groups'][0]['gross_return']==pytest.approx(0)
    # Initial capital weights, not daily rebalanced or equal terminal weights.
    assert result['groups'][0]['net_return']==pytest.approx(.999/1.001-1)
    assert result['members'][0]['net_return']==pytest.approx(1.5*.999/1.001-1)
    assert result['members'][1]['net_return']==pytest.approx(.5*.999/1.001-1)
    assert result['groups'][0]['max_drawdown'] < -.16
    assert result['benchmark_metrics']['gross_return']==pytest.approx(.5)
    assert result['groups'][0]['excess_net_return']==pytest.approx(-.5*.999/1.001)


def test_unknown_and_future_availability_do_not_fill_groups(inputs):
    signals,_=inputs
    signals['records'][0]['available_at']=None
    signals['records'][1]['available_at']='2025-01-01'
    signals['records'][2]['report_date']='2024-06-30'
    result=run(inputs)
    assert result['status']=='insufficient_samples' and result['exit_code']==1
    assert {r['reason'] for r in result['excluded_signals']}=={'unknown_availability','not_yet_available','future_report_period'}
    assert result['planned_companies']==4 and result['eligible_companies']==1


def test_zoned_availability_uses_exchange_date(inputs):
    inputs[0]['records'][0]['available_at']='2024-05-01T16:01:00Z'  # China May 2, after formation.
    result=run(inputs)
    assert result['excluded_signals'][0]['reason']=='not_yet_available'


@pytest.mark.parametrize('day',['2024-05-06','2024-05-07','2024-05-08'])
def test_missing_entry_intermediate_or_exit_price_blocks_group_without_regrouping(inputs,day):
    bars=inputs[1]['instruments'][0]['bars']
    bars[:]=[b for b in bars if b['date']!=day]
    result=run(inputs)
    assert result['exit_code']==1 and result['status']=='incomplete_prices'
    assert result['groups'][0]['net_return'] is None
    assert result['groups'][0]['members']==['000001.SZ','000002.SZ']
    assert result['groups'][1]['status']=='ok'
    assert len(result['members'])==4 and result['high_minus_low_net_return'] is None


@pytest.mark.parametrize('bad',[0,-1,float('nan'),float('inf'),True])
def test_nonpositive_nonfinite_or_boolean_price_rejected(inputs,bad):
    inputs[1]['instruments'][0]['bars'][0]['open']=bad
    with pytest.raises(ValueError):run(inputs)


def test_price_basis_duplicate_date_and_duplicate_company_rejected(inputs):
    signal,price=inputs
    clone=deepcopy(price);clone['instruments'][0]['price_basis']='unknown'
    with pytest.raises(ValueError):run((signal,clone))
    clone=deepcopy(price);clone['instruments'][0]['bars'].append(clone['instruments'][0]['bars'][0])
    with pytest.raises(ValueError):run((signal,clone))
    signal['records'].append(signal['records'][0])
    with pytest.raises(ValueError):run(inputs)


@pytest.mark.parametrize('cost',[-1,10000,True,float('nan')])
def test_invalid_cost_rejected(inputs,cost):
    with pytest.raises(ValueError):run(inputs,cost_bps=cost)


def test_rank_ties_deterministic_and_input_order_independent(inputs):
    for r in inputs[0]['records']:r['feature_value']=1
    first=run(inputs)
    inputs[0]['records'].reverse()
    assert first['groups']==run(inputs)['groups']


def test_truncated_benchmark_fails_instead_of_shortening_backtest(inputs):
    inputs[1]['instruments'][-1]['bars'].pop()
    with pytest.raises(ValueError,match='cover'):run(inputs)


def test_malformed_documents_fail_explicitly(inputs):
    for bad in ([], dict(schema='captured-adjusted-prices-v1',instruments=None),
                dict(schema='captured-adjusted-prices-v1',instruments=[None]),
                dict(schema='captured-adjusted-prices-v1',instruments=[dict(bars=[None])])):
        with pytest.raises(ValueError):run((inputs[0],bad))
    with pytest.raises(ValueError):run(([],inputs[1]))


def test_cli_outputs_and_no_overwrite(inputs,tmp_path):
    paths=[]
    for name,value in zip(('signals','prices'),inputs):
        p=tmp_path/(name+'.json');p.write_text(json.dumps(value));paths.append(str(p))
    args=SimpleNamespace(signals=paths[0],prices=paths[1],formation_date='2024-05-01',end_date='2024-05-08',
                         groups=2,cost_bps=10,benchmark='510300.SH',output_dir=tmp_path/'out')
    assert cmd_return_backtest(args)==0
    assert (args.output_dir/'groups.csv').is_file() and (args.output_dir/'nav.csv').is_file()
    assert cmd_return_backtest(args)==2
