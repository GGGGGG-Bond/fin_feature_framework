"""Export a portable, read-only result tree, without databases or response bodies."""
import argparse
from html import escape
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import uuid
from urllib.parse import unquote, urlsplit


def check_links(root):
    root=Path(root).resolve()
    missing=[]
    class Links(HTMLParser):
        def handle_starttag(self,tag,attrs):
            for key,value in attrs:
                if key not in ('href','src') or not value:continue
                url=urlsplit(value)
                if url.scheme or not url.path:continue
                candidate=(current.parent/unquote(url.path)).resolve()
                if not candidate.is_relative_to(root) or not candidate.is_file():missing.append((str(current.relative_to(root)),value))
    for current in root.rglob('*.html'):
        Links().feed(current.read_text(encoding='utf-8'))
    if missing:raise ValueError(f'broken or escaping result links: {missing}')


def export_tree(source,output):
    source,output=Path(source).resolve(),Path(output).resolve()
    if output==source or output.is_relative_to(source):raise ValueError('export must be outside runtime input')
    summary=json.loads((source/'summary.json').read_bytes())
    if summary.get('schema')!='showcase-v1' or summary.get('replay_matches')!=summary.get('replay_count'):
        raise ValueError('a successful showcase result is required')
    output.mkdir(parents=True,exist_ok=False)
    for path in sorted(source.rglob('*')):
        if path.is_symlink():raise ValueError('symlinks are not exportable')
        relative=path.relative_to(source)
        if not path.is_file() or 'raw' in relative.parts or path.name=='config.json' or path.suffix not in ('.html','.csv','.json','.md'):continue
        text=path.read_text(encoding='utf-8-sig')
        # Configs and DBs are omitted; redact output-root identity in diagnostic text.
        text=text.replace(str(source),'${DEMO_OUTPUT}')
        if re.search(r'(?:[A-Z]:[/\\]Users[/\\]|/home/|/mnt/[a-z]/|/tmp/)',text):
            raise ValueError('runtime or personal path remains in '+str(relative))
        target=output/relative;target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text(text,encoding='utf-8-sig' if path.suffix=='.csv' else 'utf-8')
    check_links(output)
    return output


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args()
    parent=Path(args.output).resolve();parent.mkdir(parents=True,exist_ok=True)
    run_id=uuid.uuid4().hex
    export_tree(args.input,parent/run_id)
    landing='<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>财务特征结果</title><h1>本次结果已生成</h1><p><a href="'+escape(run_id)+'/index.html">打开八公司、双源与价格收益完整结果</a></p><p>重建证据与数据库留在终端打印的Linux目录。</p></html>'
    (parent/'index.html').write_text(landing,encoding='utf-8')
    print('PORTABLE_RESULTS: '+str(parent/'index.html'))
    return 0


if __name__=='__main__':raise SystemExit(main())
