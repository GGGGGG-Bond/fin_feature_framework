"""Portable artifact integrity, broken-link rejection and sensitive-file exclusion."""
import hashlib
import json
from pathlib import Path
import zipfile

import pytest
from scripts.export_results import export_tree,check_links
from scripts.package_release import manifest,zip_tree,start_page,finalize_bundle,bundle_markdown
from scripts.verify_bundle import verify_bundle


@pytest.fixture
def runtime(tmp_path):
    root=tmp_path/'runtime';root.mkdir()
    (root/'summary.json').write_text(json.dumps(dict(schema='showcase-v1',replay_matches=2,replay_count=2)))
    (root/'index.html').write_text('<a href="summary.json">summary</a><a href="nested/table.csv">CSV</a>')
    (root/'nested').mkdir();(root/'nested/table.csv').write_text('a,b\n1,2\n')
    (root/'finance.db').write_bytes(b'private database')
    (root/'config.json').write_text('{"secret":"not for export"}')
    (root/'raw').mkdir();(root/'raw/body.json').write_text('{"raw":true}')
    return root


def test_portable_export_preserves_linked_results_and_omits_runtime(runtime,tmp_path):
    root=export_tree(runtime,tmp_path/'portable')
    assert (root/'summary.json').is_file() and (root/'nested/table.csv').is_file()
    assert not (root/'finance.db').exists() and not (root/'raw').exists() and not (root/'config.json').exists()
    check_links(root)
    with pytest.raises(FileExistsError):export_tree(runtime,root)
    with pytest.raises(ValueError):export_tree(runtime,runtime/'nested-export')


@pytest.mark.parametrize('href',['missing.json','../outside.json','%2e%2e/outside.json'])
def test_broken_or_escaping_report_link_rejected(runtime,tmp_path,href):
    (runtime/'index.html').write_text('<a href="'+href+'">link</a>')
    (tmp_path/'outside.json').write_text('{}')
    with pytest.raises(ValueError,match='links'):export_tree(runtime,tmp_path/'portable')


def test_sensitive_paths_and_symlinks_rejected(runtime,tmp_path):
    (runtime/'other.json').write_text(json.dumps(dict(path='/home/person/private.db')))
    with pytest.raises(ValueError,match='personal path'):export_tree(runtime,tmp_path/'portable')
    (runtime/'other.json').unlink();(runtime/'linked.json').symlink_to(runtime/'summary.json')
    with pytest.raises(ValueError,match='symlink'):export_tree(runtime,tmp_path/'another')


def test_manifest_detects_tamper_missing_and_unlisted_files(tmp_path):
    root=tmp_path/'bundle';root.mkdir();p=root/'result.txt';p.write_text('123')
    manifest(root,'0.3.0','a'*40)
    assert verify_bundle(root,strict=True)['verified_files']==1
    p.write_text('321')
    with pytest.raises(ValueError,match='changed'):verify_bundle(root)
    p.write_text('123');(root/'extra.txt').write_text('extra')
    assert verify_bundle(root)['extra_files']==['extra.txt']
    with pytest.raises(ValueError,match='unlisted'):verify_bundle(root,strict=True)
    p.unlink()
    with pytest.raises(ValueError,match='missing'):verify_bundle(root)


@pytest.mark.parametrize('path',['../secret','/etc/passwd','C:/secret','a\\b'])
def test_manifest_path_traversal_rejected(tmp_path,path):
    (tmp_path/'MANIFEST.json').write_text(json.dumps(dict(schema='fin-delivery-manifest-v1',files={path:dict(bytes=0,sha256=hashlib.sha256(b'').hexdigest())})))
    with pytest.raises(ValueError,match='unsafe'):verify_bundle(tmp_path)


def test_archive_manifest_survives_extraction_and_no_overwrite(tmp_path):
    root=tmp_path/'package';root.mkdir();(root/'START_HERE.html').write_text('<h1>ready</h1>')
    manifest(root,'0.3.0','b'*40)
    target=tmp_path/'package.zip';zip_tree(root,target)
    with zipfile.ZipFile(target) as archive:
        assert all(name.startswith('package/') for name in archive.namelist())
        archive.extractall(tmp_path/'download')
    verify_bundle(tmp_path/'download/package',strict=True)
    with pytest.raises(FileExistsError):zip_tree(root,target)


def test_start_page_for_both_bundle_types(tmp_path):
    for with_code in (False,True):
        root=tmp_path/str(with_code);root.mkdir()
        files=['results/index.html','PROJECT_BRIEF.pdf','PROJECT_BRIEF.html','GUIDE.md']
        if with_code:files+=['code/README.md','code/docs/acceptance.md','code/docs/interviewer-handoff.md']
        for name in files:
            p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('fixture')
        (root/'START_HERE.html').write_text(start_page('0.3.0','c'*40,with_code))
        result=finalize_bundle(root,'0.3.0','c'*40)
        assert result['verified_files']==len(files)+1


@pytest.mark.parametrize('with_code',[True,False])
def test_relocated_bundle_documents_resolve_to_code_or_fixed_tag(tmp_path,with_code):
    from scripts.check_docs import check
    repo=tmp_path/'repo';(repo/'docs').mkdir(parents=True)
    (repo/'README.md').write_text('# 文档索引\n',encoding='utf-8')
    (repo/'docs/acceptance.md').write_text('# 验收\n',encoding='utf-8')
    (repo/'docs/submission-message.md').write_text('# 消息\n',encoding='utf-8')
    (repo/'docs/guide.md').write_text(
        '[索引](../README.md#文档索引) · [验收](acceptance.md) · [消息](submission-message.md)\n'
        '[外部](https://example.org) · [本页](#指南)\n# 指南\n',encoding='utf-8')
    root=tmp_path/'bundle';root.mkdir()
    body=bundle_markdown(repo,'docs/guide.md','0.4.2',with_code)
    (root/'GUIDE.md').write_text(body,encoding='utf-8')
    if with_code:
        import shutil
        shutil.copytree(repo,root/'code')
        assert '(code/README.md#文档索引)' in body
        assert '(code/docs/acceptance.md)' in body
        assert '(code/docs/submission-message.md)' in body
    else:
        prefix='https://github.com/GGGGGG-Bond/fin_feature_framework/blob/v0.4.2/'
        assert '('+prefix+'README.md#文档索引)' in body
        assert '('+prefix+'docs/acceptance.md)' in body
    assert '[外部](https://example.org)' in body and '[本页](#指南)' in body
    assert check(root)['errors']==[]


def test_bundle_finalization_rejects_relocated_markdown_broken_link(tmp_path):
    (tmp_path/'GUIDE.md').write_text('[验收](acceptance.md)\n',encoding='utf-8')
    with pytest.raises(ValueError,match='Markdown'):
        finalize_bundle(tmp_path,'0.4.2','c'*40)


def test_bundle_document_relocation_rejects_escaping_source_link(tmp_path):
    (tmp_path/'docs').mkdir()
    (tmp_path/'docs/guide.md').write_text('[外部文件](../../private.md)\n',encoding='utf-8')
    with pytest.raises(ValueError,match='invalid source document link'):
        bundle_markdown(tmp_path,'docs/guide.md','0.4.2',True)
