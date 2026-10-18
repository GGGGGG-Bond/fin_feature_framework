"""Build fixed-version source, portable results and complete delivery archives.

Requires a clean Git checkout and a separately reviewed one-page PDF. Runs only
the pinned offline showcase; no financial HTTP calls or production DB access.
"""
import argparse
from datetime import datetime,timezone
import hashlib
from html import escape
import json
from pathlib import Path
import posixpath
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from urllib.parse import quote, unquote, urlsplit

if __package__:
    from .export_results import export_tree,check_links
    from .verify_bundle import verify_bundle
    from .check_docs import LINK,check as check_markdown
else:
    from export_results import export_tree,check_links
    from verify_bundle import verify_bundle
    from check_docs import LINK,check as check_markdown


def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def manifest(root,version,commit):
    root=Path(root)
    files={}
    for p in sorted(root.rglob('*')):
        if p.is_symlink():raise ValueError('symlink in bundle')
        if p.is_file() and p.name!='MANIFEST.json':files[p.relative_to(root).as_posix()]=dict(bytes=p.stat().st_size,sha256=digest(p))
    data=dict(schema='fin-delivery-manifest-v1',version=version,source_commit=commit,
              files=files,note='Immutable payload hashes; recomputation outputs are not listed. Not a third-party signature.')
    (root/'MANIFEST.json').write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return verify_bundle(root,strict=True)


def zip_tree(root,output):
    root,output=Path(root),Path(output)
    with zipfile.ZipFile(output,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as archive:
        for p in sorted(root.rglob('*')):
            if p.is_symlink():raise ValueError('symlink in archive')
            if not p.is_file():continue
            info=zipfile.ZipInfo(root.name+'/'+p.relative_to(root).as_posix(),(2026,10,18,0,0,0))
            info.external_attr=(0o100644<<16);info.compress_type=zipfile.ZIP_DEFLATED
            archive.writestr(info,p.read_bytes())


def finalize_bundle(root,version,commit):
    # START_HERE links to the manifest, so create it before validating links.
    result=manifest(root,version,commit)
    check_links(root)
    docs=check_markdown(root)
    if docs['errors']:
        raise ValueError('broken bundle Markdown links: '+str(docs['errors']))
    return result


def bundle_markdown(repo,source,version,with_code):
    """Relocate source-document links to bundled code or the fixed public tag."""
    repo,source=Path(repo),Path(source)
    body=(repo/source).read_text(encoding='utf-8')
    def relocate(match):
        group=1 if match[1] is not None else 2
        link=match[group]
        url=urlsplit(link)
        if url.scheme or not url.path:
            return match[0]
        target=posixpath.normpath(posixpath.join(source.parent.as_posix(),unquote(url.path)))
        if (target=='..' or target.startswith(('../','/')) or '\\' in target
                or not (repo/target).is_file()):
            raise ValueError('invalid source document link: '+link)
        path=quote(target,safe='/')
        prefix='code/' if with_code else ('https://github.com/GGGGGG-Bond/fin_feature_framework/blob/v'+version+'/')
        relocated=prefix+path
        if url.query:relocated+='?'+url.query
        if url.fragment:relocated+='#'+url.fragment
        start,end=match.start(group)-match.start(),match.end(group)-match.start()
        return match[0][:start]+relocated+match[0][end:]
    return LINK.sub(relocate,body)


def start_page(version,commit,with_code):
    code=('<li>复现：Linux在本目录运行 <code>bash run.sh</code>；Windows双击 <code>RUN_DEMO.cmd</code>。'
          'Linux/WSL需要Python≥3.12及venv/pip；原生Windows的Python运行命令见操作指南。首次安装需要包源网络。</li><li><a href="code/README.md">源码说明</a> · '
          '<a href="code/docs/acceptance.md">测试与验收</a> · <a href="code/docs/interviewer-handoff.md">完整操作指南</a></li>') if with_code else (
          '<li>若需复算，请下载同一Release的delivery.zip或source.zip，或按版本标签克隆源码。</li>')
    return '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>财务特征项目 · 从这里开始</title><style>body{font:17px/1.8 system-ui,sans-serif;max-width:950px;margin:40px auto;padding:0 20px;color:#203247}h1{line-height:1.3;color:#075f6a}a{color:#1767a2}.card{background:#eef5fa;padding:22px;border-radius:12px}code{font-size:14px;overflow-wrap:anywhere}.muted{color:#607385}li{margin:10px 0}</style>
<p class="muted">FINANCIAL FEATURE FRAMEWORK / '''+escape(version)+'''</p><h1>先看结果，再按需复现</h1>
<div class="card"><h2><a href="results/index.html">打开八公司、双源核验与收益结果 →</a></h2><p>解压后直接用现代浏览器打开，无需Python、Git、WSL或金融网络。包含公司/期间筛选、具体数值、净值图及CSV/JSON。</p></div>
<ol><li><a href="PROJECT_BRIEF.pdf">一页项目说明 PDF</a> · <a href="PROJECT_BRIEF.html">HTML版本</a></li><li><a href="GUIDE.md">交付与复现指南</a></li>'''+code+'''</ol>
<p>供应商视图：8公司×8季度，192报表版本，768特征 / 232有值。正式财报视图：茅台、美的、宁德时代共78项事实，目标期36/36特征有值，144/144冻结输入重放一致。两类分母分别统计。</p>
<p>96双源位置：81匹配2024年报、3对应后续调整值、12缺值/定义不齐；另含三个滚动区间、三个成本假设、三种显式样本子集的9个研究场景。</p>
<p>固定字段来自真实采样；离线HTTP信封重建，母原体、数据库及环境不随包分发。回测为手选小样本流程验证，不证明策略有效。两网站一致不证明独立上游或相同修订口径。</p>
<p>固定源码提交：<code>'''+escape(commit)+'''</code>。<a href="MANIFEST.json">逐文件校验清单</a>；复算生成的新输出不在此清单中。</p>
<p><a href="https://github.com/GGGGGG-Bond/fin_feature_framework/releases/tag/v'''+escape(version)+'''">固定版本下载</a> · <a href="https://github.com/GGGGGG-Bond/fin_feature_framework">Git历史与源码</a></p></html>'''


def build_release(repo,output,brief_pdf,version=None):
    repo,output,brief_pdf=Path(repo).resolve(),Path(output).resolve(),Path(brief_pdf).resolve()
    if not sys.platform.startswith('linux'):raise ValueError('build on Linux/WSL')
    if not brief_pdf.is_file() or not brief_pdf.read_bytes().startswith(b'%PDF-'):raise ValueError('reviewed brief PDF required')
    def git(*args):return subprocess.check_output(['git',*args],cwd=repo,text=True).strip()
    commit=git('rev-parse','HEAD')
    if git('status','--porcelain'):raise ValueError('release requires a clean Git checkout')
    project_version=tomllib.loads((repo/'pyproject.toml').read_text())['project']['version']
    if version is None:version=project_version
    if project_version!=version:raise ValueError('version mismatch')
    output.mkdir(parents=True,exist_ok=False)
    prefix='fin-feature-framework-v'+version
    with tempfile.TemporaryDirectory(prefix='fin-release-') as scratch:
        scratch=Path(scratch)
        runtime=scratch/'runtime'
        subprocess.run([sys.executable,'-m','examples.showcase_flow','--output-dir',str(runtime)],cwd=repo,
                       env={**__import__('os').environ,'PYTHONDONTWRITEBYTECODE':'1'},check=True)
        summary=json.loads((runtime/'summary.json').read_bytes())
        if (summary['features']!=768 or summary['replay_matches']!=768 or not summary['evidence_audit_clean'] or summary['backtest_status']!='success'
                or summary['reviewed_target_finite'] != 36 or summary['reviewed_replay_matches'] != 144
                or summary['return_study_status'] != 'success' or summary['return_study_scenarios'] != 9):
            raise ValueError('release showcase acceptance mismatch')
        source=output/(prefix+'-source.zip')
        subprocess.run(['git','archive','--format=zip','--prefix='+prefix+'-source/','-o',str(source),commit],cwd=repo,check=True)
        roots=[]
        for kind in ('results','delivery'):
            root=scratch/(prefix+'-'+kind);root.mkdir()
            export_tree(runtime,root/'results')
            shutil.copy2(brief_pdf,root/'PROJECT_BRIEF.pdf')
            shutil.copy2(repo/'docs/project-brief.html',root/'PROJECT_BRIEF.html')
            for document_source,document_target in [('docs/project-brief.md','PROJECT_BRIEF.md'),('docs/interviewer-handoff.md','GUIDE.md')]:
                (root/document_target).write_text(bundle_markdown(repo,document_source,version,kind=='delivery'),encoding='utf-8')
            if kind=='delivery':
                with zipfile.ZipFile(source) as archive:
                    for entry in archive.infolist():
                        relative=Path(*Path(entry.filename).parts[1:])
                        if not relative.parts:continue
                        if relative.is_absolute() or '..' in relative.parts:raise ValueError('unsafe source archive')
                        if entry.is_dir():continue
                        target=root/'code'/relative;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(archive.read(entry))
                (root/'RUN_DEMO.cmd').write_bytes(b'@echo off\r\ncall "%~dp0code\\run-demo.cmd"\r\nexit /b %errorlevel%\r\n')
                (root/'run.sh').write_text('#!/usr/bin/env bash\nset -euo pipefail\ncd "$(dirname "$0")/code"\nexec bash run.sh "$@"\n',encoding='utf-8')
            (root/'START_HERE.html').write_text(start_page(version,commit,kind=='delivery'),encoding='utf-8')
            (root/'OPEN_RESULTS.cmd').write_bytes(b'@echo off\r\nstart "" "%~dp0START_HERE.html"\r\n')
            roots.append(finalize_bundle(root,version,commit))
            zip_tree(root,output/(root.name+'.zip'))
        pdf=output/('project-brief-v'+version+'.pdf');shutil.copy2(brief_pdf,pdf)
        record=dict(schema='fin-release-v1',version=version,tag='v'+version,source_commit=commit,
                    built_at=datetime.now(timezone.utc).isoformat(),showcase=summary,
                    bundle_checks=roots,financial_requests_during_build=0,
                    test_record='docs/acceptance.md; packaging validation recorded separately',
                    assets={p.name:dict(bytes=p.stat().st_size,sha256=digest(p)) for p in sorted(output.iterdir()) if p.is_file()})
        (output/'release-manifest.json').write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        files=sorted(p for p in output.iterdir() if p.is_file())
        (output/'SHA256SUMS.txt').write_text(''.join(digest(p)+'  '+p.name+'\n' for p in files),encoding='ascii')
        return record


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output-dir',required=True);parser.add_argument('--brief-pdf',required=True)
    args=parser.parse_args()
    record=build_release(Path(__file__).resolve().parents[1],args.output_dir,args.brief_pdf)
    print(json.dumps(dict(source_commit=record['source_commit'],assets=record['assets']),indent=2))


if __name__=='__main__':main()
