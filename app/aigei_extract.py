"""Extract media explicitly supplied by public Aigei pages; no download API."""
import base64,hashlib,json,math,os,re,shutil,uuid
from pathlib import Path
from urllib.parse import urljoin,urlsplit,urlencode
from bs4 import BeautifulSoup
from .downloads import DownloadError,DownloadCancelled,stream_file,media_info,safe_name,binary,run_process,apply_cookies
from .net import HttpClient,NetworkError

EXTENSIONS={'video':{'.mp4','.mov','.m4v','.webm','.mkv'},'sound':{'.mp3','.wav','.ogg','.oga','.flac','.m4a','.aac','.opus','.wma'},
            'image':{'.jpg','.jpeg','.png','.gif','.webp','.avif'}}

class BrowserMediaRequired(DownloadError):
    needs_browser=True

def playback_variant(row,data):
    """Validate browser output again on the desktop; never trust arbitrary URLs."""
    path=page_path(row.url)
    if not isinstance(data,dict) or data.get('pagePath')!=path:raise BrowserMediaRequired('浏览器返回的资源与当前条目不符，请重新播放目标资源')
    url=media_url(data.get('url',''),'sound')
    if not url:raise BrowserMediaRequired('目标播放器未提供受支持的爱给音频文件地址')
    try:
        duration=float(data.get('durationSeconds',0));limit=float(data.get('playbackLimitSeconds',0))
    except (ValueError,TypeError):raise BrowserMediaRequired('网页音频时长无效，请刷新目标页面后重试')
    if not math.isfinite(duration) or not 0<=duration<=86400 or not math.isfinite(limit) or limit<0:
        raise BrowserMediaRequired('网页音频时长无效，请刷新目标页面后重试')
    if limit>0 and (not duration or limit<duration-1):
        raise BrowserMediaRequired('当前网页播放器限制了试听长度，不能作为完整播放版提取。请在原站处理播放限制后重试。')
    label='网页播放版'+(' · 约 %d:%02d'%(int(round(duration))//60,int(round(duration))%60) if duration else '（时长待校验）')
    return {'key':hashlib.sha256(('browser-playback:'+path).encode()).hexdigest()[:24],
            'url':url,'label':label,'preview':True,'browser_playback':True,'expected_duration':duration,
            'automatic_browser':data.get('automatic_browser') is True}

def browser_variants(row,settings,cancel):
    owned=getattr(settings,'_aigei_browser',None)
    if owned is not None and owned.active and not getattr(settings,'_aigei_auto_start',False):
        if hasattr(owned,'refresh_ready'):owned.refresh_ready(cancel)
    if owned is not None and (owned.active or getattr(settings,'_aigei_auto_start',False)):
        try:return [playback_variant(row,owned.extract(page_path(row.url),cancel,getattr(settings,'_download_progress',lambda _:None)))]
        except DownloadCancelled:raise
        except Exception as exc:raise BrowserMediaRequired(str(exc)) from exc
    browser=getattr(settings,'_login_browser',None)
    if browser is None or not browser.active:
        raise BrowserMediaRequired('请点击“一键打开并提取”，工具会自动打开资源网页并播放识别。')
    try:return [playback_variant(row,browser.aigei_media(page_path(row.url),cancel))]
    except Exception as exc:
        if cancel.is_set():raise DownloadCancelled('已取消') from exc
        if isinstance(exc,BrowserMediaRequired):raise
        raise BrowserMediaRequired('浏览器播放提取未完成：'+str(exc)+'。请点击一键按钮重试。') from exc

def page_path(url):
    try:
        p=urlsplit(url)
        if p.scheme not in ('http','https') or p.netloc.lower() not in ('www.aigei.com','aigei.com'):return ''
        return p.path if re.fullmatch(r'/item/[A-Za-z0-9_-]+\.html',p.path) else ''
    except ValueError:return ''

def media_url(value,kind):
    try:
        url=urljoin('https://www.aigei.com/',str(value).strip());p=urlsplit(url);host=(p.hostname or '').lower()
        if p.scheme!='https' or p.username or p.password or p.port not in (None,443):return ''
        if not any(host==d or host.endswith('.'+d) for d in ('aigei.com','5open.com')):return ''
        if not p.path.startswith(('/src/','/pvimg/','/pvaud/','/pvvdo/')):return ''
        return url if Path(p.path).suffix.lower() in EXTENSIONS[kind] else ''
    except (ValueError,TypeError):return ''

def parse_variants(document,row,listing=False):
    """Listing fallback must match the exact resource page, not a similar title."""
    soup=BeautifulSoup(document,'html.parser');root=soup;path=page_path(row.url)
    if not path:raise DownloadError('不是有效的爱给独立资源页面')
    scoped=None
    for box in soup.select('.unit-container'):
        link=box.select_one('.item-view-url a[href]')
        if link and page_path(urljoin('https://www.aigei.com/',link.get('href','')))==path:scoped=box;break
    if scoped is not None:root=scoped
    elif listing:return []
    else:
        canonical=soup.select_one('link[rel="canonical"][href]');og=soup.select_one('meta[property="og:url"][content]')
        declared=(canonical.get('href') if canonical else og.get('content') if og else '')
        if page_path(urljoin(row.url,declared))!=path or not declared:return []
        # An unrecognized detail layout must not collect recommendation players.
        primary=BeautifulSoup('','html.parser')
        tag={'video':'video','sound':'audio','image':'img'}[row.resource_type]
        nodes=soup.select(tag+'[src],'+tag+' source[src]')
        if len(nodes)==1:primary.append(nodes[0])
        prop={'video':'og:video','sound':'og:audio','image':'og:image'}[row.resource_type]
        media=soup.select_one('meta[property="'+prop+'"][content]')
        if media:
            node=primary.new_tag(tag);node['src']=media.get('content');primary.append(node)
        root=primary
    kind=row.resource_type;variants=[];seen=set()
    def add(value,label,preview=True,width=0,height=0):
        url=media_url(value,kind)
        if not url:return
        key=hashlib.sha256((kind+urlsplit(url).path+str(width)+':'+str(height)).encode()).hexdigest()[:24]
        if key in seen:return
        seen.add(key);variants.append({'key':key,'url':url,'label':label,'preview':preview,'width':width,'height':height})
    if kind=='video':
        for node in root.select('[fast-video-url]'):
            try:
                url=base64.b64decode(re.sub(r'\s','',node['fast-video-url']),validate=True).decode('utf-8')
                width=int(node.get('fast-video-width',0));height=int(node.get('fast-video-height',0))
            except (ValueError,UnicodeError):continue
            add(url,'网页预览视频'+(' · %d×%d'%(width,height) if width and height else ''),width=width,height=height)
    if kind in ('video','sound'):
        tag='video' if kind=='video' else 'audio'
        for node in root.select(tag+'[src],'+tag+' source[src]'):
            add(node.get('src'),'网页播放文件（完整性未确认）' if kind=='video' else '网页试听音频（完整性未确认）')
        for node in root.select('a[download][href]'):
            add(node.get('href'),'网页提供的媒体文件（完整性未确认）',preview=False)
        for script in root.select('script[type="application/ld+json"]'):
            try:objects=json.loads(script.string or script.get_text())
            except (ValueError,TypeError):continue
            objects=objects if isinstance(objects,list) else [objects]
            for item in objects:
                if not isinstance(item,dict):continue
                if item.get('@type')==('VideoObject' if kind=='video' else 'AudioObject'):
                    add(item.get('contentUrl',''),'网页播放文件（完整性未确认）')
    elif kind=='image':
        from .adapters.sound import cover_url
        add(cover_url(root),'网页预览图（可能含缩放或水印）')
    return variants

def resolve(row,settings,auth,cancel,*,browser_only=False):
    if cancel.is_set():raise DownloadCancelled('已取消')
    if not page_path(row.url) or row.resource_type not in EXTENSIONS:raise DownloadError('此资源未接入爱给网页提取')
    if row.resource_type=='sound' and (browser_only or getattr(settings,'_aigei_auto_start',False) or
            getattr(getattr(settings,'_aigei_browser',None),'active',False) or getattr(getattr(settings,'_login_browser',None),'active',False)):
        return browser_variants(row,settings,cancel)
    http=HttpClient(settings);http.cancel_event=cancel;errors=[]
    try:
        apply_cookies(http,auth,row.url)
        query=str((row.extra or {}).get('aigei_query') or row.title).strip()[:200]
        try:page=max(1,min(10000,int((row.extra or {}).get('aigei_page',1))))
        except (ValueError,TypeError):page=1
        pages=[]
        if query:pages.append(('https://www.aigei.com/s?'+urlencode({'q':query,'type':{'video':'video','sound':'sound','image':'pic'}[row.resource_type],'page':page}),True))
        pages.append(('https://www.aigei.com'+page_path(row.url),False))
        for url,listing in pages:
            if cancel.is_set():raise DownloadCancelled('已取消')
            try:
                document=http.get_text(url,headers={'Referer':'https://www.aigei.com/'},timeout=30)
                found=parse_variants(document,row,listing)
                if found:return found
            except NetworkError as error:
                if cancel.is_set():raise DownloadCancelled('已取消') from error
                errors.append(str(error))
        label='音频' if row.resource_type=='sound' else '媒体'
        if row.resource_type=='sound':return browser_variants(row,settings,cancel)
        raise DownloadError('公开页面未提供可提取的%s文件地址；没有调用爱给下载接口。'%label+(' 页面请求：'+'；'.join(errors) if errors else ''))
    finally:http.close_all()

def inspect_resource(row,settings,auth,cancel):
    variants=resolve(row,settings,auth,cancel)
    return {'kind':'web_'+row.resource_type,'title':row.title,'count':1,'web_variants':variants,
            'note':('已识别浏览器正常播放版本；下载后校验时长和实际音质。播放版不等于原始高质量文件，转成WAV/FLAC不会提升源音质。'
                    if any(v.get('browser_playback') for v in variants) else
                    '从网页提取到可保存的文件。请查看版本说明：预览/试听可能缩短、压缩或带水印，未验证为原文件。下载不调用 MCP。')}

def verify_playback(source,chosen,cancel,notify):
    notify({'phase':'校验播放版时长与音质','percent':-1})
    lines=run_process([binary('ffprobe'),'-v','error','-show_entries',
        'format=duration:stream=codec_type,codec_name,sample_rate,bit_rate','-of','json',str(source)],cancel)
    try:
        data=json.loads('\n'.join(lines));duration=float(data['format']['duration'])
        audio=[s for s in data['streams'] if s.get('codec_type')=='audio']
        if not math.isfinite(duration) or duration<=0 or len(audio)!=1 or any(s.get('codec_type')=='video' for s in data['streams']):raise ValueError()
    except (KeyError,ValueError,TypeError):raise DownloadError('播放版音频元数据校验失败，未保存')
    expected=chosen.get('expected_duration',0)
    if expected and abs(duration-expected)>max(1.5,expected*.005):
        raise DownloadError('音频时长与网页不符（文件%.2f秒，网页%.2f秒），可能截短或已切换版本，未保存。'%(duration,expected))
    # Probe metadata alone does not detect damaged packets in the middle of a file.
    run_process([binary('ffmpeg'),'-hide_banner','-loglevel','error','-xerror','-err_detect','explode',
        '-i',str(source),'-map','0:a:0','-vn','-f','null','-'],cancel)
    stream=audio[0];details=['网页播放版','%.2f秒'%duration]
    try:
        if int(stream.get('bit_rate',0))>0:details.append('%dkbps'%round(int(stream['bit_rate'])/1000))
        if int(stream.get('sample_rate',0))>0:details.append('%sHz'%stream['sample_rate'])
    except (ValueError,TypeError):pass
    details.append('时长与网页相符' if expected else '网页时长未知')
    notify({'phase':'播放版校验通过','extraction_label':' · '.join(details),'percent':-1})

def download_resource(row,settings,auth,options,folder,cancel,notify):
    key=options.get('aigei_variant')
    if not key:raise DownloadError('请重新解析网页并选择提取版本，旧版下载任务不能直接沿用')
    browser_key=hashlib.sha256(('browser-playback:'+page_path(row.url)).encode()).hexdigest()[:24]
    candidates=resolve(row,settings,auth,cancel,browser_only=key==browser_key);chosen=next((v for v in candidates if v['key']==key),None)
    if chosen is None:raise DownloadError('网页提供的文件版本已变化，请重新解析')
    work=Path(folder)/('.aigei-web-'+uuid.uuid4().hex[:8]);work.mkdir();http=HttpClient(settings);http.cancel_event=cancel
    try:
        apply_cookies(http,auth,row.url);suffix=Path(urlsplit(chosen['url']).path).suffix.lower();source=work/('extracted'+suffix)
        notify({'phase':'提取下载 · '+chosen['label'],'percent':-1})
        if chosen.get('browser_playback'):
            try:stream_file(http,chosen['url'],source,'https://www.aigei.com/',cancel,notify,1,1)
            except NetworkError as exc:
                if cancel.is_set():raise DownloadCancelled('已取消') from exc
                if getattr(exc,'status',0) in (401,403,410):raise BrowserMediaRequired('播放地址已失效，请刷新资源网页并正常播放，再重新解析或重试下载') from exc
                raise
            verify_playback(source,chosen,cancel,notify)
        else:stream_file(http,chosen['url'],source,'https://www.aigei.com/',cancel,notify,1,1)
        prefix='[网页播放版] ' if chosen.get('browser_playback') else '[网页预览] ' if chosen['preview'] else '[网页提取] '
        name=safe_name(prefix+row.title)
        if row.resource_type=='sound':
            from .sound_download import finish_audio
            from dataclasses import replace
            return finish_audio(source,replace(row,title=prefix+row.title),options,folder,cancel,notify)
        if row.resource_type=='video':
            streams=media_info(source,cancel)
            if not any(s.get('codec_type')=='video' for s in streams):raise DownloadError('提取文件没有可验证的视频轨道')
            fmt=options.get('container','mp4')
            if fmt not in ('mp4','mkv'):raise DownloadError('不支持的视频保存格式')
            converted=work/('saved.'+fmt)
            run_process([binary('ffmpeg'),'-hide_banner','-loglevel','error','-y','-i',str(source),'-map','0:v:0','-map','0:a?','-c','copy','-sn','-dn',str(converted)],cancel)
            if not any(s.get('codec_type')=='video' for s in media_info(converted,cancel)):raise DownloadError('保存后的视频校验失败')
            source=converted
        if cancel.is_set():raise DownloadCancelled('已取消')
        target=Path(folder)/(name+source.suffix);os.replace(source,target);return [str(target)]
    finally:http.close_all();shutil.rmtree(work,ignore_errors=True)
