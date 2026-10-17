import copy
import hashlib
import json
from types import SimpleNamespace
import pytest
from fin_feature.reconcile import compare_documents, compare_files, cmd_reconcile, FIELDS


def documents():
    rows = []
    for field, (table, basis) in FIELDS.items():
        rows.append(dict(secucode='600519.SH', report_date='2024-12-31', field=field,
                         table_type=table, period_basis=basis, value='10000', unit='yuan',
                         currency='CNY', scope='consolidated', comparison_basis_id='fixture-original',
                         alignment_evidence='ARTIFICIAL matching original reports',
                         record_ref='fixture-row-' + field, evidence_ref='ARTIFICIAL fixture'))
    left = dict(schema='reconcile-v1', source='fixture-a', upstream='fixture-a', records=rows)
    right = copy.deepcopy(left)
    right.update(source='fixture-b', upstream='fixture-b')
    for row in right['records']:
        row.update(value='1', unit='wan_yuan')
    return left, right


def test_units_and_no_mutation():
    a, b = documents()
    before = copy.deepcopy((a, b))
    result = compare_documents(a, b)
    assert result['counts'] == {'match': 6}
    assert result['comparable_fraction'] == 1
    assert (a, b) == before
    assert result['evidence_verified'] is False


@pytest.mark.parametrize('key,value', [('currency','USD'), ('scope','unknown'),
    ('comparison_basis_id','revision-2'), ('period_basis','flow_single_quarter'),
    ('unit','unknown'), ('alignment_evidence',''), ('evidence_ref',None)])
def test_incomparable(key, value):
    a, b = documents(); b['records'][0][key] = value
    result = compare_documents(a, b)['results']
    assert sum(r['status'] == 'incomparable' for r in result) == 1


def test_missing_difference_and_zero():
    a, b = documents()
    b['records'][0]['value'] = '2'
    b['records'][1]['value'] = None
    b['records'].pop()
    result = compare_documents(a, b)
    assert result['counts'] == {'match': 3, 'missing': 2, 'difference': 1}
    a['records'][0]['value'] = '-0.5'; b['records'][0].update(value='0',unit='yuan')
    assert next(r for r in compare_documents(a,b)['results'] if r['field']=='total_operate_income')['status']=='match'


@pytest.mark.parametrize('value', [True, 'NaN', 'Infinity', 'bad'])
def test_invalid_values(value):
    a,b=documents(); b['records'][0]['value']=value
    assert compare_documents(a,b)['counts']['missing']==1


def test_reject_ambiguous_and_same_upstream():
    a,b=documents(); b['upstream']=a['upstream']
    with pytest.raises(ValueError,match='same upstream'): compare_documents(a,b)
    b['upstream']='other'; b['records'].append(b['records'][0])
    with pytest.raises(ValueError,match='duplicate'): compare_documents(a,b)


@pytest.mark.parametrize('tol', ['-1', 'NaN', 'bad'])
def test_bad_tolerance(tol):
    with pytest.raises(ValueError): compare_documents(*documents(),abs_tol=tol)


def test_files_cli_and_immutability(tmp_path, capsys):
    a,b=documents()
    paths=[tmp_path/'a.json',tmp_path/'b.json']
    for path,doc in zip(paths,(a,b)): path.write_text(json.dumps(doc))
    hashes=[hashlib.sha256(p.read_bytes()).hexdigest() for p in paths]
    args=SimpleNamespace(left=paths[0],right=paths[1],abs_tol='1',rel_tol='0.000001')
    assert cmd_reconcile(args)==0
    report=json.loads(capsys.readouterr().out)
    assert report['input_sha256']==hashes
    assert [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths]==hashes
    b['records'][0]['value']='2';paths[1].write_text(json.dumps(b))
    assert cmd_reconcile(args)==1
    capsys.readouterr()
    args.right=tmp_path/'missing.json'
    assert cmd_reconcile(args)==2
    assert not args.right.exists()


def test_cli_subprocess(tmp_path):
    import subprocess,sys
    paths=[tmp_path/'a.json',tmp_path/'b.json']
    for path,doc in zip(paths,documents()): path.write_text(json.dumps(doc))
    result=subprocess.run([sys.executable,'-m','fin_feature.cli','reconcile',
        '--left',str(paths[0]),'--right',str(paths[1])],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)['counts']=={'match':6}
