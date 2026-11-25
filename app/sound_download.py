"""Direct public audio files and local audio format conversion."""
import os,shutil,uuid
from pathlib import Path
from urllib.parse import urlsplit
from .downloads import DownloadError,DownloadCancelled,safe_name,stream_file,media_info,binary,run_process
from .net import HttpClient

AUDIO_EXTS={'.mp3','.wav','.ogg','.oga','.flac','.m4a','.aac','.opus','.wma','.aiff'}

def inspect_sound(row,settings,cancel):
    if cancel.is_set():raise DownloadCancelled('已取消')
    return {'kind':'sound','title':row.title,'count':1,'note':'下载当前公开音频文件，可保留原始格式或转换。'}

def finish_audio(source,row,options,folder,cancel,notify):
    streams=media_info(source,cancel)
    if not any(s['codec_type']=='audio' for s in streams) or any(s['codec_type']=='video' for s in streams):
        raise DownloadError('文件不是独立音频，未保存为音效')
    fmt=options.get('audio_format','best')
    if fmt not in ('best','mp3','m4a','wav','flac'):raise DownloadError('不支持的音频格式')
    target=Path(folder)/(safe_name(row.title)+(source.suffix if fmt=='best' else '.'+fmt))
    if fmt=='best':
        if cancel.is_set():raise DownloadCancelled('已取消')
        os.replace(source,target)
    else:
        notify({'phase':'转换音频','percent':-1});partial=source.parent/('converted.'+fmt)
        args=[binary('ffmpeg'),'-hide_banner','-loglevel','error','-y','-i',str(source),'-vn']
        if fmt=='mp3':args+=['-codec:a','libmp3lame','-q:a','0']
        elif fmt=='m4a':args+=['-codec:a','aac','-b:a','192k']
        args+=[str(partial)];run_process(args,cancel)
        result=media_info(partial,cancel)
        if not any(s['codec_type']=='audio' for s in result):raise DownloadError('转换后的音频校验失败')
        if cancel.is_set():raise DownloadCancelled('已取消')
        os.replace(partial,target)
    return [str(target)]

def download_sound(row,settings,auth,options,folder,cancel,notify):
    work=Path(folder)/('.audio-'+uuid.uuid4().hex[:8]);work.mkdir()
    try:
        source=work/('original'+Path(urlsplit(row.url).path).suffix.lower());http=HttpClient(settings);http.cancel_event=cancel
        try:
            from .downloads import apply_cookies
            apply_cookies(http,auth,row.url)
            stream_file(http,row.url,source,row.extra.get('page_url') or row.url,cancel,notify,1,1)
        finally:http.close_all()
        files=finish_audio(source,row,options,folder,cancel,notify);return files
    finally:shutil.rmtree(work,ignore_errors=True)
