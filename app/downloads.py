"""Portable, per-resource download backend. No shell commands or global config."""
import json,os,re,subprocess,threading,time,uuid
from pathlib import Path
from urllib.parse import urlsplit,parse_qs
from http.cookiejar import MozillaCookieJar
from .config import app_dir,Settings
from .net import HttpClient,detect_proxy

class DownloadError(RuntimeError):pass
class DownloadCancelled(DownloadError):pass

_process_lock=threading.RLock()
_processes={}

def cancel_processes(events):
    """Stop only download children before the GUI process exits."""
    with _process_lock:
        for event in events:
            event.set()
            for process in list(_processes.get(event,())):stop_process(process)

def tools_dir():
    return Path(app_dir())/'vendor'/'download-tools'

def binary(name):
    path=tools_dir()/(name+'.exe')
    if not path.is_file():raise DownloadError('缺少内置下载组件：'+name+'。请使用完整解压的新版工具。')
    return str(path)

def platform(row):
    url=row.url
    try:parsed=urlsplit(url);host=(parsed.hostname or '').lower()
    except ValueError:return ''
    if parsed.scheme not in ('http','https') or parsed.username or parsed.password:return ''
    if row.resource_type in ('video','image','sound') and host in ('www.aigei.com','aigei.com') and re.fullmatch(r'/item/[A-Za-z0-9_-]+\.html',parsed.path):return 'aigei-web'
    if row.resource_type=='sound':
        if Path(parsed.path).suffix.lower() in ('.mp3','.wav','.ogg','.oga','.flac','.m4a','.aac','.opus','.wma','.aiff'):return 'sound'
    origin=(row.extra or {}).get('page_url','')
    try:oh=(urlsplit(origin).hostname or '').lower()
    except ValueError:oh=''
    if row.resource_type=='video':
        if host in ('www.bilibili.com','bilibili.com','m.bilibili.com','b23.tv') and ('/video/' in url or host=='b23.tv'):return 'bilibili'
        if host in ('youtube.com','www.youtube.com','m.youtube.com','youtu.be') and ('/watch' in url or '/shorts/' in url or host=='youtu.be'):return 'youtube'
    if host in ('pixiv.net','www.pixiv.net') and re.search(r'/artworks/\d+|/novel/show\.php\?',url):return 'pixiv'
    if row.resource_type=='article':return 'article'
    if row.resource_type=='image' and oh in ('bilibili.com','www.bilibili.com') and any(host==d or host.endswith('.'+d) for d in ('hdslb.com','biliimg.com')):return 'bili-image'
    return ''

def safe_name(text):
    name=re.sub(r'[\x00-\x1f<>:"/\\|?*]','_',str(text)).strip(' .')[:90].rstrip(' .') or 'resource'
    if re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)',name,re.I):name='_'+name
    return name

def redact(text):
    return re.sub(r'https?://\S+',lambda m:m.group(0).split('?')[0],str(text))[-3500:]

def stop_process(process):
    if process.poll() is not None:return
    if os.name=='nt':
        subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW,timeout=8)
    else:process.terminate()

def run_process(args,cancel,notify=lambda _:None,timeout=180):
    with _process_lock:
        if cancel.is_set():raise DownloadCancelled('已取消')
        p=subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL,
            encoding='utf-8',errors='replace',creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        _processes.setdefault(cancel,set()).add(p)
    last=[time.monotonic()];timed=[False];done=threading.Event();lines=[];size=0
    def watch():
        while not done.wait(.2):
            if cancel.is_set() or time.monotonic()-last[0]>timeout:
                timed[0]=not cancel.is_set()
                try:stop_process(p)
                except Exception:pass
                return
    threading.Thread(target=watch,daemon=True).start()
    try:
        for line in p.stdout:
            last[0]=time.monotonic();line=line.rstrip();size+=len(line)
            if size>12*1024*1024:
                stop_process(p);raise DownloadError('下载组件返回的数据过大')
            lines.append(line);notify(line)
        code=p.wait()
    finally:
        done.set();p.stdout.close()
        if p.poll() is None:stop_process(p)
        with _process_lock:
            _processes.get(cancel,set()).discard(p)
            if not _processes.get(cancel):_processes.pop(cancel,None)
    if cancel.is_set():raise DownloadCancelled('已取消')
    if timed[0]:raise DownloadError('下载组件连续3分钟无响应，可检查网络后重试')
    if code:
        error=redact('\n'.join(lines[-14:])) or '下载组件退出码 '+str(code)
        if 'Could not copy Chrome cookie database' in error:error='所选浏览器的登录数据库被占用或无法读取。可关闭浏览器后重试，或用内置登录助手导出该平台Cookie文件。\n\n'+error
        elif 'Sign in to confirm' in error:error='平台要求登录验证。请先在浏览器完成登录，再选择浏览器登录态或Cookie文件重新解析。\n\n'+error
        raise DownloadError(error)
    return lines

def base_args(settings,site,auth):
    args=[binary('yt-dlp'),'--ignore-config','--no-cache-dir','--no-playlist','--playlist-items','1','--no-colors',
          '--encoding','utf-8','--no-warnings','--socket-timeout','30','--retries','2','--fragment-retries','2',
          '--ffmpeg-location',str(tools_dir()),'--js-runtimes','deno:'+binary('deno'),
          '--no-remote-components']
    proxy=detect_proxy(settings) if site=='youtube' or settings.proxy_mode=='manual' else ''
    args+=['--proxy',proxy or '']
    if auth.get('browser') in ('chrome','edge','firefox'):args+=['--cookies-from-browser',auth['browser']]
    elif auth.get('file'):
        if not Path(auth['file']).is_file():raise DownloadError('Cookie文件不存在，请重新选择')
        # yt-dlp writes cookie jars; use a task-local copy, never modify user's file.
        args+=['--cookies',auth['file']]
    return args

def cookie_copy(auth,work):
    auth=dict(auth)
    if auth.get('file'):
        import shutil
        dest=Path(work)/'session.cookies'
        try:shutil.copyfile(auth['file'],dest)
        except Exception:
            dest.unlink(missing_ok=True);raise
        auth['file']=str(dest)
    return auth

def inspect_resource(row,settings,auth,cancel):
    import tempfile
    site=platform(row)
    if not site:raise DownloadError('此资源尚未接入下载，请在支持下载的资源封面右键操作')
    if site=='aigei-web':
        from .aigei_extract import inspect_resource as inspect_aigei
        return inspect_aigei(row,settings,auth,cancel)
    if site=='sound':
        from .sound_download import inspect_sound
        return inspect_sound(row,settings,cancel)
    if site=='article':
        from .article_download import fetch_article
        article=fetch_article(row,settings,auth,cancel)
        return {'kind':'text','title':article['title'],'count':1,'note':'保存当前页面已公开的正文，不自动下载章节链接或整个系列。'}
    if site in ('bilibili','youtube'):
        with tempfile.TemporaryDirectory(prefix='rf-inspect-') as work:
            lines=run_process(base_args(settings,site,cookie_copy(auth,work))+['--skip-download','--dump-single-json','--',row.url],cancel)
        try:info=json.loads(next(l for l in reversed(lines) if l.startswith('{')))
        except (ValueError,StopIteration):raise DownloadError('未解析到视频格式，请查看登录或平台限制提示')
        if info.get('entries'):
            info=next((e for e in info['entries'] if e),{})
        formats=[f for f in info.get('formats',[]) if not f.get('has_drm') and f.get('url')]
        heights=sorted({int(f['height']) for f in formats if f.get('height') and f.get('vcodec')!='none'},reverse=True)
        video=any(f.get('vcodec') not in (None,'none') for f in formats)
        audio=any(f.get('acodec') not in (None,'none') for f in formats)
        if not video and not audio:raise DownloadError('没有可下载的普通媒体格式，内容或编码可能受限')
        return {'title':info.get('title') or row.title,'kind':'video','heights':heights,'has_video':video,'has_audio':audio,
                'duration':info.get('duration'), 'count':1,'note':'仅下载当前视频/当前分P，不展开整个合集。画质以平台实际提供为准。'}
    if site=='bili-image':return {'kind':'image','title':row.title,'count':1,'note':'下载此条资源的图片地址，保留来源提供的格式。'}
    h=HttpClient(settings);h.cancel_event=cancel;apply_cookies(h,auth)
    try:
        match=re.search(r'/artworks/(\d+)',row.url)
        if match:
            data=h.get_json('https://www.pixiv.net/ajax/illust/'+match[1],headers={'Referer':row.url})
            if data.get('error'):raise DownloadError(str(data.get('message') or 'Pixiv需要登录或作品不可用'))
            body=data.get('body') or {}
            return {'kind':'ugoira' if body.get('illustType')==2 else 'image','title':body.get('title') or row.title,
                    'count':int(body.get('pageCount') or 1),'note':'按作品重新解析原图；多图作品可下载全部图片。'}
        identity=parse_qs(urlsplit(row.url).query).get('id',[''])[0]
        if not identity.isdigit():raise DownloadError('无法识别Pixiv小说ID')
        data=h.get_json('https://www.pixiv.net/ajax/novel/'+identity,headers={'Referer':row.url})
        if data.get('error') or not (data.get('body') or {}).get('content'):raise DownloadError(str(data.get('message') or '未取得小说正文，可能需要登录'))
        return {'kind':'text','title':data['body'].get('title') or row.title,'count':1,'note':'保存当前可访问的正文，不自动下载整个系列。'}
    finally:h.close_all()

def apply_cookies(http,auth,url='https://www.pixiv.net/'):
    if auth.get('browser'):raise DownloadError('Pixiv请使用公开作品或显式选择Cookie文件，当前不自动读取浏览器登录态')
    if auth.get('file'):
        http.settings=Settings.from_dict(http.settings.to_dict());http.settings.insecure_fallback=False
        jar=MozillaCookieJar(auth['file']);jar.load(ignore_discard=True,ignore_expires=False)
        # CookieJar honors secure/path/expiry and strips cookies on unrelated redirects.
        import copy
        host=(urlsplit(url).hostname or '').lower();selected=[]
        for cookie in jar:
            domain=cookie.domain.lstrip('.').lower()
            if domain==host or (domain.count('.')>=1 and host.endswith('.'+domain)):
                item=copy.copy(cookie);item.domain=host;item.domain_initial_dot=False;selected.append(item)
        if not selected:raise DownloadError('登录文件中没有当前平台的有效凭据，请重新登录后导入')
        old=http._session
        def session(proxy):
            result=old(proxy)
            for cookie in selected:result.cookies.set_cookie(cookie)
            return result
        http._session=session

def selector(mode,height):
    bound='[height<=%d]'%height if height else ''
    if mode=='audio':return 'bestaudio/best'
    if mode=='silent':return 'bestvideo'+bound+'/best'+bound
    return 'bestvideo*'+bound+'+bestaudio/best'+bound+'/bestvideo'+bound

def media_info(path,cancel):
    lines=run_process([binary('ffprobe'),'-v','error','-show_entries','stream=codec_type,codec_name,width,height','-of','json',str(path)],cancel)
    return json.loads('\n'.join(lines)).get('streams',[])

def download_video(row,settings,auth,options,folder,cancel,notify):
    work=folder/'.work';work.mkdir(exist_ok=True)
    args=base_args(settings,platform(row),auth)+['--newline','--progress','--progress-template','download:RF_PROGRESS:%(progress)j',
        '--print','after_move:RF_FILE:%(filepath)j','--no-simulate','--continue','--no-overwrites',
        '-P',str(work),'-o','media.%(ext)s','-f',selector(options['mode'],options.get('height',0))]
    mode=options['mode'];container=options.get('container','mp4')
    if mode=='audio':args+=['-x','--audio-format',options.get('audio_format','best'),'--audio-quality','0']
    else:args+=['--merge-output-format',container,'--remux-video',container]
    if options.get('subtitles'):args+=['--write-subs','--write-auto-subs','--sub-langs','zh.*,en.*','--sub-format','srt/best','--convert-subs','srt']
    auth=cookie_copy(auth,work)
    if auth.get('file'):args[args.index('--cookies')+1]=auth['file']
    files=[]
    def progress(line):
        if line.startswith('RF_PROGRESS:'):
            try:
                d=json.loads(line[12:]);total=d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                notify({'phase':'下载中','percent':min(98,int(d.get('downloaded_bytes',0)*100/total)) if total else -1,
                        'speed':d.get('speed') or 0,'bytes':d.get('downloaded_bytes') or 0})
            except (ValueError,TypeError,ZeroDivisionError):pass
        elif line.startswith('RF_FILE:'):
            try:files.append(Path(json.loads(line[8:])))
            except ValueError:pass
        elif line.startswith('['):notify({'phase':'解析 / 合并 / 转换中','detail':redact(line)})
    try:run_process(args+['--',row.url],cancel,progress)
    finally:
        if auth.get('file'):
            Path(auth['file']).unlink(missing_ok=True)
    media=next((p for p in reversed(files) if p.exists() and p.resolve().is_relative_to(work.resolve())),None)
    if media is None:raise DownloadError('下载器未返回完整媒体文件')
    if mode=='silent':
        notify({'phase':'移除音轨','percent':-1});silent=work/('silent.'+container)
        run_process([binary('ffmpeg'),'-hide_banner','-loglevel','error','-y','-i',str(media),'-map','0:v:0','-an','-c:v','copy',str(silent)],cancel)
        media=silent
    streams=media_info(media,cancel);types={s.get('codec_type') for s in streams}
    if mode=='audio' and ('audio' not in types or 'video' in types):raise DownloadError('音频输出校验失败')
    if mode=='silent' and ('video' not in types or 'audio' in types):raise DownloadError('无声视频输出校验失败')
    if mode=='full' and 'video' not in types:raise DownloadError('完整视频输出校验失败')
    if mode=='full' and options.get('require_audio') and 'audio' not in types:raise DownloadError('完整视频缺少预期的音轨，请重试')
    if cancel.is_set():raise DownloadCancelled('已取消')
    target=folder/(safe_name(row.title)+media.suffix);os.replace(media,target)
    for path in work.glob('*.srt'):os.replace(path,folder/path.name)
    # Remove only task-owned residual media after a successful verified output.
    import shutil
    shutil.rmtree(work)
    return [str(target)]

def stream_file(http,url,target,referer,cancel,notify,index,total):
    part=target.with_suffix(target.suffix+'.part');done=0
    if cancel.is_set():raise DownloadCancelled('已取消')
    with http.get(url,headers={'Referer':referer},stream=True,timeout=15) as response:
        size=int(response.headers.get('Content-Length') or 0)
        if 'text/html' in response.headers.get('Content-Type',''):raise DownloadError('服务器返回网页而非资源文件，可能需要登录')
        started=time.monotonic()
        with part.open('wb') as f:
            for chunk in response.iter_content(65536):
                if cancel.is_set():raise DownloadCancelled('已取消')
                if not chunk:continue
                f.write(chunk);done+=len(chunk)
                notify({'phase':'下载 %d/%d'%(index,total),'percent':int(((index-1)+(done/size if size else 0))*100/total),'speed':done/max(.1,time.monotonic()-started),'bytes':done})
    if not done or (size and done!=size):raise DownloadError('资源文件未完整下载')
    if target.suffix.lower() in ('.jpg','.jpeg','.png','.webp','.gif','.avif'):
        from PIL import Image
        try:
            with Image.open(part) as img:img.verify()
        except Exception as exc:raise DownloadError('图片文件校验失败：'+str(exc))
    if cancel.is_set():raise DownloadCancelled('已取消')
    os.replace(part,target)

def download_native(row,settings,auth,options,folder,cancel,notify):
    if platform(row)=='aigei-web':
        from .aigei_extract import download_resource
        return download_resource(row,settings,auth,options,folder,cancel,notify)
    if platform(row)=='sound':
        from .sound_download import download_sound
        return download_sound(row,settings,auth,options,folder,cancel,notify)
    if platform(row)=='article':
        from .article_download import fetch_article,save_text
        article=fetch_article(row,settings,auth,cancel)
        return save_text(article['title'],row.url,article['text'],folder,options.get('text_format','txt'),cancel)
    h=HttpClient(settings);h.cancel_event=cancel;apply_cookies(h,auth);files=[]
    try:
        if platform(row)=='bili-image':urls=[row.url]
        else:
            match=re.search(r'/artworks/(\d+)',row.url)
            if not match:
                identity=parse_qs(urlsplit(row.url).query)['id'][0]
                data=h.get_json('https://www.pixiv.net/ajax/novel/'+identity,headers={'Referer':row.url})
                text=(data.get('body') or {}).get('content')
                if data.get('error') or not text:raise DownloadError(str(data.get('message') or '未取得正文'))
                if cancel.is_set():raise DownloadCancelled('已取消')
                from .article_download import save_text
                return save_text(row.title,row.url,text,folder,options.get('text_format','txt'),cancel)
            identity=match[1]
            if options.get('kind')=='ugoira':
                data=h.get_json('https://www.pixiv.net/ajax/illust/'+identity+'/ugoira_meta',headers={'Referer':row.url})
                body=data.get('body') or {};url=body.get('originalSrc') or body.get('src')
                if data.get('error') or not url:raise DownloadError(str(data.get('message') or '未取得动图'))
                host=urlsplit(url).hostname or ''
                if urlsplit(url).scheme!='https' or not host.endswith('.pximg.net'):raise DownloadError('动图地址不属于Pixiv图床')
                target=folder/'animation.zip';stream_file(h,url,target,row.url,cancel,notify,1,1)
                timing=folder/'frames.json';timing.write_text(json.dumps(body.get('frames',[]),ensure_ascii=False,indent=2),encoding='utf-8')
                fmt=options.get('ugoira_format','zip')
                if fmt in ('mp4','gif'):
                    output=convert_ugoira(target,body.get('frames',[]),fmt,cancel,notify)
                    target.unlink();timing.unlink();return [str(output)]
                import zipfile
                with zipfile.ZipFile(target) as archive:
                    if archive.testzip():raise DownloadError('动图压缩包校验失败')
                return [str(target),str(timing)]
            data=h.get_json('https://www.pixiv.net/ajax/illust/'+identity+'/pages',headers={'Referer':row.url})
            if data.get('error') or not isinstance(data.get('body'),list):raise DownloadError(str(data.get('message') or '未取得原图'))
            urls=[p.get('urls',{}).get('original') for p in data['body']]
            if options.get('pages')=='first':urls=urls[:1]
            if not urls or not all(urls):raise DownloadError('作品缺少原图地址')
        for i,url in enumerate(urls,1):
            host=urlsplit(url).hostname or ''
            allowed=('pximg.net',) if platform(row)=='pixiv' else ('hdslb.com','biliimg.com')
            if urlsplit(url).scheme!='https' or not any(host==d or host.endswith('.'+d) for d in allowed):raise DownloadError('资源下载地址不属于所选平台')
            suffix=Path(urlsplit(url).path).suffix.lower()
            if suffix not in ('.jpg','.jpeg','.png','.webp','.gif','.avif'):raise DownloadError('暂不支持该图片文件格式')
            target=folder/('%03d'%i+suffix)
            stream_file(h,url,target,row.extra.get('page_url') or row.url,cancel,notify,i,len(urls));files.append(str(target))
        return files
    finally:h.close_all()

def convert_ugoira(archive,frames,fmt,cancel,notify):
    import zipfile,shutil
    if fmt not in ('mp4','gif') or not isinstance(frames,list) or not 0<len(frames)<=2000:raise DownloadError('动图帧数或格式不受支持')
    work=archive.parent/'.frames';work.mkdir(exist_ok=True);concat=[]
    with zipfile.ZipFile(archive) as z:
        if sum(i.file_size for i in z.infolist())>1024**3:raise DownloadError('动图展开体积超过1GB，请选择原始帧包')
        for i,frame in enumerate(frames):
            if cancel.is_set():raise DownloadCancelled('已取消')
            name=frame.get('file','');delay=float(frame.get('delay',0))
            if not re.fullmatch(r'\d+\.(?:jpg|jpeg|png)',name) or not 0<delay<=60000:raise DownloadError('动图帧名称或时长异常')
            target=work/('%06d'%i+Path(name).suffix);target.write_bytes(z.read(name))
            concat+=['file '+target.name,'duration %.6f'%(delay/1000)]
        concat+=['file '+target.name]
    listing=work/'frames.txt';listing.write_text('\n'.join(concat)+'\n',encoding='utf-8')
    notify({'phase':'转换动图为 '+fmt.upper(),'percent':-1});partial=archive.parent/('animation.partial.'+fmt)
    args=[binary('ffmpeg'),'-hide_banner','-loglevel','error','-y','-f','concat','-safe','1','-i',str(listing)]
    if fmt=='mp4':args+=['-vf','pad=ceil(iw/2)*2:ceil(ih/2)*2','-c:v','libx264','-pix_fmt','yuv420p','-fps_mode','vfr','-movflags','+faststart']
    else:args+=['-filter_complex','[0:v]split[a][b];[a]palettegen[p];[b][p]paletteuse','-loop','0','-fps_mode','vfr']
    run_process(args+['-an',str(partial)],cancel)
    if not partial.is_file() or partial.stat().st_size==0:raise DownloadError('动图转换未产生有效文件')
    if cancel.is_set():raise DownloadCancelled('已取消')
    final=archive.parent/('animation.'+fmt);os.replace(partial,final);shutil.rmtree(work);return final
