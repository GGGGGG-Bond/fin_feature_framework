"""Offline local Markdown link/heading check; no package dependencies or HTTP."""
import argparse
from html import unescape
import json
import os
from pathlib import Path
import re
from urllib.parse import unquote,urlsplit

SKIP={'.git','.venv','venv','data','reports','logs','output','outputs','dist','build','node_modules','__pycache__','.codex','.agents'}
LINK=re.compile(r'!?\[[^\]\n]+\]\(\s*(?:<([^>]+)>|([^\s)]+))(?:\s+"[^"\n]*")?\s*\)')


def visible_markdown(text):
    result=[];fence=None
    for line in text.splitlines():
        match=re.match(r'^\s*(`{3,}|~{3,})',line)
        if match:
            mark=match[1][0]
            if fence is None:fence=mark
            elif fence==mark:fence=None
            continue
        if fence is None:result.append(line)
    return '\n'.join(result)


def anchors(text):
    counts={};result=set()
    for line in visible_markdown(text).splitlines():
        match=re.match(r'^#{1,6}\s+(.+?)(?:\s+#+)?\s*$',line)
        if not match:continue
        title=re.sub(r'<[^>]+>','',unescape(match[1])).lower()
        title=re.sub(r'[^\w\s-]','',title)
        slug=re.sub(r'\s','-',title)
        index=counts.get(slug,0);counts[slug]=index+1
        result.add(slug+(f'-{index}' if index else ''))
    result.update(re.findall(r'<a\s+(?:id|name)=["\']([^"\']+)',text,re.I))
    return result


def check(root):
    root=Path(root).resolve();documents=[];errors=[]
    if not root.is_dir():errors.append(dict(file='.',link=str(root),reason='root_not_directory'))
    for current,dirs,files in os.walk(root):
        dirs[:]=sorted(d for d in dirs if d not in SKIP and not d.endswith('.egg-info'))
        documents.extend(Path(current)/name for name in sorted(files) if name.endswith('.md'))
    if root.is_dir() and not documents:errors.append(dict(file='.',link='',reason='no_markdown_documents'))
    local_links=0;heading_cache={}
    for document in documents:
        body=visible_markdown(document.read_text(encoding='utf-8-sig'))
        for match in LINK.finditer(body):
            link=match[1] or match[2]
            url=urlsplit(link)
            if url.scheme in ('http','https','mailto'):continue
            local_links+=1
            if url.scheme or link.startswith(('/','\\')):
                errors.append(dict(file=str(document.relative_to(root)),link=link,reason='nonportable_local_link'));continue
            target=(document.parent/unquote(url.path)).resolve() if url.path else document
            if not target.is_relative_to(root) or not target.exists():
                errors.append(dict(file=str(document.relative_to(root)),link=link,reason='missing_or_escaping_target'));continue
            if url.fragment and target.suffix=='.md':
                if target not in heading_cache:heading_cache[target]=anchors(target.read_text(encoding='utf-8-sig'))
                if unquote(url.fragment) not in heading_cache[target]:
                    errors.append(dict(file=str(document.relative_to(root)),link=link,reason='missing_markdown_heading'))
    return dict(schema='documentation-check-v1',markdown_files=len(documents),local_links=local_links,
                errors=errors,external_http_requests=0,scope='Markdown inline local paths and Markdown heading anchors; no external availability check')


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',default=str(Path(__file__).resolve().parents[1]))
    args=parser.parse_args();result=check(args.root);print(json.dumps(result,ensure_ascii=False,indent=2))
    return 1 if result['errors'] else 0


if __name__=='__main__':raise SystemExit(main())
