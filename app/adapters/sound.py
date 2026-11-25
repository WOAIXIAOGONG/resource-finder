"""Public Aigei resource listings. No playback or paid-download requests."""
import base64,re
from urllib.parse import urlencode,urljoin,urlsplit
from bs4 import BeautifulSoup
from .pagination import PageResults
from .generic import GenericAdapter
from ..models import SearchResult,is_web_url
from ..net import NetworkError
from ..sources import matches

def cover_url(box):
    for image in box.select('img'):
        value=image.get('data-original') or image.get('src','')
        if value.startswith('aigei-image-encode-'):
            try:value=base64.b64decode(re.sub(r'\s','',value[len('aigei-image-encode-'):]),validate=True).decode('utf-8')
            except (ValueError,UnicodeError):continue
        value=urljoin('https://www.aigei.com/',value)
        host=urlsplit(value).hostname or ''
        if is_web_url(value) and ((matches(host,'aigei.com') and '/src/img/' in value) or (matches(host,'5open.com') and '/pvimg/img/' in value)):return value
    return ''

def aigei(ctx,site,page,kind='sound'):
    if kind not in ('sound','video','image'):raise NetworkError('爱给网此类型尚未支持')
    url='https://www.aigei.com/s?'+urlencode({'q':ctx.query,'type':{'sound':'sound','video':'video','image':'pic'}[kind],'page':page})
    soup=BeautifulSoup(ctx.http.get_text(url,site),'html.parser')
    current=soup.select_one('.pageInfo_currentPage');last=soup.select_one('.pageInfo_totalPage')
    if current and current.get_text(strip=True).isdigit() and int(current.get_text(strip=True))!=page:
        raise NetworkError('爱给网未返回请求的页码，可再次触底重试；尚未确认末页')
    rows=[];seen=set()
    selector={'sound':'.unit-container-restype-sound','video':'.unit-container-restype-videoVideo,.unit-container-restype-videoTemplate',
              'image':'.unit-container:has(.unit-content-box.image-box)'}[kind]
    for box in soup.select(selector):
        if kind=='image' and any(c.startswith('unit-container-restype-video') or c=='unit-container-restype-sound' for c in box.get('class',[])):continue
        if ctx.cancelled():raise NetworkError('已停止')
        link=box.select_one('.item-view-url a[href]')
        if link is None:continue
        target=urljoin(url,link.get('href',''));parsed=urlsplit(target)
        if not is_web_url(target) or not matches(parsed.hostname or '','aigei.com') or not parsed.path.startswith('/item/'):continue
        if target in seen:continue
        title=box.select_one('.title-name,.trans-title');title=title.get_text('',strip=True) if title else link.get('title') or link.get_text(' ',strip=True)
        if not title:continue
        author=box.select_one('.author');author=(author.get('title') or author.get_text(' ',strip=True).removeprefix('作者：')) if author else ''
        cover=cover_url(box)
        tags=' · '.join(t.get_text(' ',strip=True) for t in box.select('.unittags-container .tags'))
        rows.append(SearchResult(title=title,url=target,cover=cover,author=author,resource_type=kind,
            source=site.display,source_id=site.id,desc=tags[:300],extra={'page_url':target,'image_page':kind=='image','discovery_sources':[site.id],
                'aigei_query':ctx.query,'aigei_page':page}))
        seen.add(target)
    empty=soup.select_one('.search-no-result,.no-result,.no-result-content')
    ended=bool(last and last.get_text(strip=True).isdigit() and page>=int(last.get_text(strip=True)))
    if not rows and not (empty or (last and last.get_text(strip=True)=='0')):
        raise NetworkError('爱给网未返回对应类型条目，可能需要验证或页面结构变化；尚未确认末页')
    note=enrich(ctx,rows,page,kind)
    if note and (ended or bool(empty)):note='当前来源没有更多公开检索结果；'+note
    result=PageResults(rows,exhausted=ended or bool(empty),message=note);result.next_offset=page+1;result.next_url=''
    return result

def enrich(ctx,rows,page,kind):
    """Official metadata only augments independently verified website resources."""
    if not rows or not hasattr(ctx.http,'post_json'):return ''
    media={'sound':'audio','video':'video','image':'image'}[kind]
    args={'keyword':ctx.query,'cat':{'sound':'sound','video':'video','image':'image'}[kind],'pageNumber':page}
    client=getattr(getattr(ctx.http,'settings',None),'_aigei_mcp',None)
    try:
        if client and client.connected():data=client.call(media+'-search',args,ctx.cancel)
        else:
            result=ctx.http.post_json('https://www.aigei.com/open/api/v1/'+media+'/search',json=args)
            if not isinstance(result,dict) or str(result.get('code'))!='10001':return '爱给官方元数据暂不可用，已保留公开页面结果'
            data=result.get('data',{})
        def key(title,author):return (''.join(str(title or '').split()).casefold(),''.join(str(author or '').split()).casefold())
        indexed={};by_title={}
        for item in (data or {}).get('itemList',[]):
            title,author=key(item.get('itemTitle'),item.get('work',{}).get('workAuthor'))
            indexed.setdefault((title,author),[]).append(item);by_title.setdefault(title,[]).append(item)
        for row in rows:
            items=indexed.get(key(row.title,row.author),[])
            if not row.author:items=by_title.get(key(row.title,'')[0],[])
            if len(items)!=1:continue
            item=items[0]
            if not row.cover:
                previews=item.get('prevImageList',[])
                if previews:
                    url=previews[0].get('prevMiniUrl','');host=urlsplit(url).hostname or ''
                    if is_web_url(url) and (matches(host,'aigei.com') or matches(host,'5open.com')):row.cover=url
            from datetime import datetime
            try:row.pub_ts=datetime.strptime(item.get('work',{}).get('workUploadDate',''),'%Y-%m-%d').timestamp()
            except (ValueError,TypeError):pass
            props=(item.get('fileList') or [{}])[0].get(media+'Properties',{})
            seconds=props.get(media+'DurationSeconds')
            if isinstance(seconds,(int,float)) and seconds>0:row.duration='%d:%02d'%(int(seconds)//60,int(seconds)%60)
        return ''
    except Exception:
        if ctx.cancelled():raise NetworkError('已停止')
        return '爱给官方元数据暂不可用，已保留公开页面结果；可在设置检查 MCP 连接'
