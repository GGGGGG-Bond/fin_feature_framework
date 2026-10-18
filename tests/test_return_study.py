"""Public edition selection, repeated trading costs and complete denominators."""
from copy import deepcopy
import json
from types import SimpleNamespace
import pytest
from fin_feature.addons.return_study import run_return_study, select_public_signals, cmd_return_study


@pytest.fixture
def study_inputs():
    codes = ['000001.SZ', '000002.SZ', '000003.SZ', '000004.SZ']
    signals = dict(schema='return-signals-v1', feature_name='factor', records=[dict(secucode=code,
        report_date='2023-12-31', available_at='2024-04-30', feature_value=i, evidence_ref='reviewed-report') for i, code in enumerate(codes)])
    prices = dict(schema='captured-adjusted-prices-v1', instruments=[dict(secucode=code, price_basis='provider_qfq', bars=[
        dict(date='2024-05-06', open=100, close=100), dict(date='2024-05-07', open=100, close=110),
        dict(date='2024-05-08', open=200, close=200), dict(date='2024-05-09', open=200, close=240)]) for code in codes + ['510300.SH']])
    plan = dict(schema='return-study-plan-v1', windows=[dict(formation_date='2024-05-01', end_date='2024-05-07'),
        dict(formation_date='2024-05-07', end_date='2024-05-09')], universes=[dict(name='four', companies=codes, groups=2)], cost_grid_bps=[0, 10, 30])
    return signals, prices, plan


def run(inputs):
    signals, prices, plan = inputs
    return run_return_study(signals, prices, windows=plan['windows'], universes=plan['universes'], costs=plan['cost_grid_bps'])


def test_chaining_reforms_groups_and_charges_each_window_both_sides(study_inputs):
    result = run(study_inputs)
    assert result['exit_code'] == 0 and len(result['scenarios']) == 3
    previous = float('inf')
    for scenario in result['scenarios']:
        fee = scenario['cost_bps_per_side'] / 10000
        expected = 1.1 * 1.2 * ((1-fee)/(1+fee))**2 - 1
        assert scenario['chained_metrics']['G1']['net_return'] == pytest.approx(expected)
        assert scenario['chained_metrics']['benchmark']['net_return'] == pytest.approx(expected)
        assert expected < previous
        previous = expected
        nav = [r for r in scenario['chained_nav'] if r['series'] == 'G1']
        assert len(nav) == 5 and len({p['date'] for p in nav}) == 5
        # Open jumps from 110 to 200 across windows: cash does not earn this gap.
        assert nav[-1]['nav'] < 1.33


def test_later_public_signal_changes_only_later_formation(study_inputs):
    signals, _, plan = study_inputs
    newer = {**signals['records'][0], 'report_date':'2024-03-31', 'available_at':'2024-05-07', 'feature_value':99}
    signals['records'].append(newer)
    result = run(study_inputs)['scenarios'][0]
    assert result['windows'][0]['groups'][0]['members'] == ['000001.SZ', '000002.SZ']
    assert result['windows'][1]['groups'][0]['members'] == ['000002.SZ', '000003.SZ']
    assert result['windows'][1]['groups'][1]['members'] == ['000004.SZ', '000001.SZ']


@pytest.mark.parametrize('availability', [None, '2025-01-01'])
def test_missing_or_future_signal_does_not_shrink_planned_denominator(study_inputs, availability):
    study_inputs[0]['records'][0]['available_at'] = availability
    result = run(study_inputs)
    assert result['exit_code'] == 1
    assert all(s['status'] == 'incomplete' and not s['chained_nav'] for s in result['scenarios'])
    assert all(len(s['companies']) == 4 for s in result['scenarios'])
    assert result['scenarios'][0]['windows'][0]['formation_missing_signals'][0]['reason'] == 'no_public_signal_at_formation'


def test_missing_member_price_never_chains_partial_success(study_inputs):
    study_inputs[1]['instruments'][0]['bars'].pop()
    result = run(study_inputs)
    assert result['exit_code'] == 1
    assert result['scenarios'][0]['windows'][0]['exit_code'] == 0
    assert result['scenarios'][0]['windows'][1]['exit_code'] == 1
    assert not result['scenarios'][0]['chained_metrics']


@pytest.mark.parametrize('change', ['missing_value', 'nonfinite_value', 'missing_evidence'])
def test_invalid_selected_signal_blocks_chaining_even_when_enough_other_names_remain(study_inputs, change):
    row = study_inputs[0]['records'][0]
    if change == 'missing_evidence':
        row['evidence_ref'] = ''
    else:
        row['feature_value'] = None if change == 'missing_value' else float('nan')
    if change == 'nonfinite_value':
        with pytest.raises(ValueError):
            run(study_inputs)  # Invalid JSON numbers must never enter exports.
        return
    result = run(study_inputs)
    assert result['exit_code'] == 1
    for scenario in result['scenarios']:
        assert len(scenario['companies']) == 4 and not scenario['chained_nav']
        assert scenario['windows'][0]['eligible_companies'] == 3
        assert scenario['windows'][0]['excluded_signals']


def test_incorrect_signal_schema_is_not_relabelled_as_valid(study_inputs):
    study_inputs[0]['schema'] = 'unreviewed-other-format'
    with pytest.raises(ValueError, match='return-signals-v1'):
        run(study_inputs)


@pytest.mark.parametrize('formation', ['2024-05-06', '2024-05-08'])
def test_overlapping_or_gapped_boundaries_rejected(study_inputs, formation):
    study_inputs[2]['windows'][1]['formation_date'] = formation
    with pytest.raises(ValueError, match='adjacent'):
        run(study_inputs)


def test_ambiguous_equally_dated_editions_are_not_resolved_by_input_order(study_inputs):
    signals, _, plan = study_inputs
    signals['records'].append({**signals['records'][0], 'feature_value':999})
    with pytest.raises(ValueError, match='ambiguous'):
        select_public_signals(signals, '2024-05-01', plan['universes'][0]['companies'])


def test_cli_writes_complete_research_and_refuses_overwrite(study_inputs, tmp_path):
    paths = []
    for name, value in zip(('signals', 'prices', 'plan'), study_inputs):
        path = tmp_path / (name + '.json'); path.write_text(json.dumps(value)); paths.append(path)
    args = SimpleNamespace(signals=paths[0], prices=paths[1], plan=paths[2], output_dir=tmp_path/'study')
    assert cmd_return_study(args) == 0
    assert (args.output_dir/'sensitivity.csv').is_file()
    assert cmd_return_study(args) == 2
