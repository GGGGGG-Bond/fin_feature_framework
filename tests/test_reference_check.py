import copy
import hashlib
import json
from types import SimpleNamespace
import pytest
from fin_feature.reference_check import check_reference,cmd_reference_check


def setup_files(tmp_path):
    row=dict(secucode='600519.SH',report_date='2024-12-31',field='total_assets',
             value='100.00003',table_type='BS',period_basis='stock_end',currency='unknown',
             unit='unknown',scope='unknown',comparison_basis_id='unknown',
             record_ref='fixture-row',evidence_ref='fixture-response')
    a=dict(schema='reconcile-v1',source='a',upstream='a',records=[row])
    b=copy.deepcopy(a);b.update(source='b',upstream='b');b['records'][0]['value']='100'
    paths=[tmp_path/'a.json',tmp_path/'b.json',tmp_path/'ref.json',tmp_path/'doc.pdf']
    for p,d in zip(paths,(a,b)):p.write_text(json.dumps(d))
    paths[3].write_bytes(b'%PDF-FIXTURE not a real financial document')
    ref=dict(schema='reviewed-reference-v1',reviewed_by='ARTIFICIAL reviewer',
        document=dict(url='https://example.test/fixture.pdf',sha256=sha(paths[3])),
        input_sha256=[sha(p) for p in paths[:2]],currency='CNY',unit='yuan',scope='consolidated',
        source_units={'a':'yuan','b':'yuan'},absolute_tolerance='0.01',
        records=[dict(row,value='100',page=1,row_label='ARTIFICIAL total assets',column_label='2024')])
    paths[2].write_text(json.dumps(ref))
    return paths


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def edit(path,callback):
    d=json.loads(path.read_text());callback(d);path.write_text(json.dumps(d))


def test_matching_rounding_does_not_change_unknown_semantics(tmp_path):
    paths=setup_files(tmp_path);before=[sha(p) for p in paths]
    result=check_reference(*paths)
    assert result['counts']=={'match_reference':1}
    assert result['historical_visibility_verified'] is False
    assert result['provider_revision_alignment_verified'] is False
    assert result['document_hash_verified'] is True
    assert before==[sha(p) for p in paths]
    assert result['results'][0]['providers']['a']['original']['unit']=='unknown'


@pytest.mark.parametrize('index',[0,1,3])
def test_changed_inputs_or_pdf_rejected(tmp_path,index):
    paths=setup_files(tmp_path);paths[index].write_bytes(paths[index].read_bytes()+b' ')
    with pytest.raises(ValueError,match='hash'):check_reference(*paths)


@pytest.mark.parametrize('mutation',['scope','currency','unit','tolerance','page','row','coverage'])
def test_bad_reference_rejected(tmp_path,mutation):
    paths=setup_files(tmp_path)
    def change(d):
        if mutation in ('scope','currency','unit'):d[mutation]='unknown'
        elif mutation=='tolerance':d['absolute_tolerance']='1'
        elif mutation=='page':d['records'][0]['page']=True
        elif mutation=='row':d['records'][0]['row_label']=''
        elif mutation=='coverage':d['records'].append(dict(d['records'][0],field='total_equity'))
    edit(paths[2],change)
    with pytest.raises(ValueError):check_reference(*paths)


@pytest.mark.parametrize('value,status',[('102','difference_reference'),(None,'missing')])
def test_real_difference_and_missing_stay_visible(tmp_path,value,status):
    paths=setup_files(tmp_path);edit(paths[0],lambda d:d['records'][0].update(value=value))
    edit(paths[2],lambda d:d.update(input_sha256=[sha(p) for p in paths[:2]]))
    report=check_reference(*paths)
    assert report['counts']=={status:1}
    assert report['results'][0]['providers']['b']['status']=='match_reference'


def test_explicit_scope_conflict_cannot_be_overridden(tmp_path):
    paths=setup_files(tmp_path);edit(paths[0],lambda d:d['records'][0].update(scope='parent_only'))
    edit(paths[2],lambda d:d.update(input_sha256=[sha(p) for p in paths[:2]]))
    with pytest.raises(ValueError,match='metadata conflicts'):check_reference(*paths)


def test_cli_exit_codes(tmp_path,capsys):
    p=setup_files(tmp_path);args=SimpleNamespace(left=p[0],right=p[1],reference=p[2],document=p[3])
    assert cmd_reference_check(args)==0
    assert json.loads(capsys.readouterr().out)['total']==1
    edit(p[0],lambda d:d['records'][0].update(value='102'))
    edit(p[2],lambda d:d.update(input_sha256=[sha(x) for x in p[:2]]))
    assert cmd_reference_check(args)==1
    capsys.readouterr();args.document=tmp_path/'missing.pdf'
    assert cmd_reference_check(args)==2
    assert not args.document.exists()


def test_selected_fields_are_explicit_and_unchecked_counted(tmp_path):
    paths=setup_files(tmp_path)
    for p in paths[:2]:
        edit(p,lambda d:d['records'].append(dict(d['records'][0],field='total_equity')))
    edit(paths[2],lambda d:d.update(input_sha256=[sha(p) for p in paths[:2]]))
    with pytest.raises(ValueError,match='same exact identities'):check_reference(*paths)
    edit(paths[2],lambda d:d.update(coverage='selected-fields'))
    result=check_reference(*paths)
    assert result['total']==1 and result['input_pairs']==2 and result['unchecked_pairs']==1
    assert result['coverage']=='selected-fields'


@pytest.mark.parametrize('mode',['auto','',True])
def test_unsupported_coverage_rejected(tmp_path,mode):
    p=setup_files(tmp_path);edit(p[2],lambda d:d.update(coverage=mode))
    with pytest.raises(ValueError,match='coverage'):check_reference(*p)


@pytest.mark.parametrize('unit,printed',[('million_yuan','0.0001'),('wan_yuan','0.01'),('yuan','100')])
def test_explicit_printed_unit_conversion(tmp_path,unit,printed):
    p=setup_files(tmp_path)
    edit(p[2],lambda d:d['records'][0].update(document_unit=unit,document_value=printed))
    assert check_reference(*p)['counts']=={'match_reference':1}


@pytest.mark.parametrize('unit,printed',[('million_yuan','100'),('unknown','100'),('yuan',None)])
def test_bad_document_units_or_wrong_conversion_rejected(tmp_path,unit,printed):
    p=setup_files(tmp_path)
    edit(p[2],lambda d:d['records'][0].update(document_unit=unit,document_value=printed))
    with pytest.raises(ValueError):check_reference(*p)
