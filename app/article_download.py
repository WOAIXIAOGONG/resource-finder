"""Save the current accessible article only, never a whole paid/linked series."""
import html,re,os
from pathlib import Path
from urllib.parse import urlsplit
from bs4 import BeautifulSoup
from .downloads import DownloadError,DownloadCancelled,safe_name,apply_cookies
from .net import HttpClient

class MissingArticle(DownloadError):pass

def extract_article(markup,title=''):
    soup=BeautifulSoup(markup,'html.parser')
    for node in soup.select('script,style,noscript,nav,header,footer,form,button,aside,iframe'):
        node.decompose()
    plain=soup.get_text(' ',strip=True)
    if len(plain)<500 and re.search(r'登录后(?:查看|阅读)|请(?:先)?登录.*(?:阅读|查看)|sign in to (?:read|continue)',plain,re.I):
        raise DownloadError('请先登录后阅读此文章')
    selectors=['.read-article-holder','.opus-module-content','.opus-para-graph','#mw-content-text .mw-parser-output',
               '[itemprop="articleBody"]','.article-content','.article-body','.entry-content','#content .chaptercontent',
               '#chaptercontent','article','main']
    blocks=[]
    for selector in selectors:
        nodes=soup.select(selector)
        if not nodes:continue
        text='\n\n'.join(n.get_text('\n',strip=True) for n in nodes)
        if len(re.sub(r'\s','',text))>=80:
            blocks.append(text);break
    if not blocks:
        raise MissingArticle('未识别到可下载正文：页面没有公开正文、仅有摘要，或使用了尚未支持的动态排版。不会将导航/搜索结果冒充正文。')
    text=blocks[0]
    # Explicit login gates are not article content. A normal article may discuss logging in.
    if len(text)<500 and re.search(r'登录后(?:查看|阅读)|请(?:先)?登录.*(?:阅读|查看)|sign in to (?:read|continue)',text,re.I):
        raise DownloadError('请先登录后阅读此文章')
    heading=soup.select_one('.opus-module-title') or soup.find('h1') or soup.find('title')
    return {'title':heading.get_text(' ',strip=True) if heading else title,'text':text}

def fetch_article(row,settings,auth,cancel):
    http=HttpClient(settings);http.cancel_event=cancel
    apply_cookies(http,auth,row.url)
    try:
        if cancel.is_set():raise DownloadCancelled('已取消')
        url=row.url;parsed=urlsplit(url)
        match=re.fullmatch(r'/read/cv(\d+)/?',parsed.path)
        if parsed.hostname in ('www.bilibili.com','bilibili.com') and match:
            # The official desktop page is a JS shell; its own mobile link redirects to public opus HTML.
            url='https://www.bilibili.com/read/mobile?id='+match[1]
        for attempt in range(2):
            with http.get(url,stream=True,headers={'Referer':row.url}) as response:
                content_type=response.headers.get('Content-Type','').lower()
                if 'text/html' not in content_type and 'application/xhtml' not in content_type:
                    raise DownloadError('当前链接不是可解析的文章网页')
                chunks=[];size=0
                for chunk in response.iter_content(65536):
                    if cancel.is_set():raise DownloadCancelled('已取消')
                    size+=len(chunk)
                    if size>8*1024*1024:raise DownloadError('文章页面超过8MB，暂不支持直接保存')
                    chunks.append(chunk)
                body=b''.join(chunks)
                # Explicit isolated self-test diagnostic; never dump an authenticated response.
                import sys,json
                if not auth and '--selftest' in sys.argv and '--selfarticleout' in sys.argv:
                    pos=sys.argv.index('--selfarticleout')
                    if pos+1<len(sys.argv):
                        dump=Path(sys.argv[pos+1]);dump.parent.mkdir(parents=True,exist_ok=True);dump.write_bytes(body)
                        dump.with_suffix('.json').write_text(json.dumps({'url':response.url,'encoding':response.encoding,'content_type':content_type,'bytes':len(body)},ensure_ascii=False),encoding='utf-8')
                # Respect HTTP charset or let the HTML parser read the meta charset.
                encoding=response.encoding if 'charset=' in content_type else None
                try:result=extract_article(body.decode(encoding,errors='replace') if encoding else body,row.title)
                except MissingArticle:
                    target=urlsplit(response.url)
                    if attempt==0 and parsed.hostname in ('www.bilibili.com','bilibili.com') and target.hostname in ('www.bilibili.com','bilibili.com') and target.scheme=='https' and target.path.startswith('/opus/'):
                        url=response.url;continue
                    raise
                if cancel.is_set():raise DownloadCancelled('已取消')
                return result
    finally:http.close_all()

def save_text(title,url,text,folder,fmt,cancel):
    if cancel.is_set():raise DownloadCancelled('已取消')
    fmt='html' if fmt=='html' else 'txt'
    path=Path(folder)/(safe_name(title)+'.'+fmt);part=path.with_suffix(path.suffix+'.part')
    content=title+'\n'+url+'\n\n'+text
    if fmt=='html':
        content='<!doctype html><meta charset="utf-8"><title>'+html.escape(title)+'</title><style>body{max-width:860px;margin:48px auto;padding:0 24px;font:18px/1.8 sans-serif}pre{white-space:pre-wrap;font:inherit}</style><h1>'+html.escape(title)+'</h1><p>'+html.escape(url)+'</p><pre>'+html.escape(text)+'</pre>'
    part.write_text(content,encoding='utf-8-sig')
    if cancel.is_set():raise DownloadCancelled('已取消')
    os.replace(part,path);return [str(path)]
