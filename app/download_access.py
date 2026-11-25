"""Anonymous first; bounded login fallback, shared by inspection and transfer."""
import os
from pathlib import Path
from .downloads import DownloadError,DownloadCancelled,platform

class LoginRequired(DownloadError):pass

def needs_login(error):
    text=str(error).lower()
    return any(s in text for s in ('sign in to confirm','login required','login is required','log in to','sign in to','please log in',
        '需要登录','请先登录','请登录','登录后','http 401','not logged in','ログイン'))

def access_key(row):
    from urllib.parse import urlsplit
    host=(urlsplit(row.url).hostname or '').lower()
    for domain in ('youtube.com','bilibili.com','pixiv.net'):
        if host==domain or host.endswith('.'+domain):return domain
    if host=='youtu.be':return 'youtube.com'
    if host=='b23.tv':return 'bilibili.com'
    return host

def candidates(row,preferred):
    values=[]
    if preferred.get('file') and Path(preferred['file']).is_file():values.append({'file':preferred['file']})
    if platform(row) in ('youtube','bilibili'):
        local=Path(os.environ.get('LOCALAPPDATA',''));roaming=Path(os.environ.get('APPDATA',''))
        found=[name for name,path in [('edge',local/'Microsoft/Edge/User Data'),('chrome',local/'Google/Chrome/User Data'),
                                     ('firefox',roaming/'Mozilla/Firefox/Profiles')] if path.is_dir()]
        preferred_browser=preferred.get('browser')
        if preferred_browser in found:found.remove(preferred_browser);found.insert(0,preferred_browser)
        values.extend({'browser':b} for b in found)
    return values[:3]

def automatic(row,preferred,cancel,operation,notify=lambda _:None):
    if cancel.is_set():raise DownloadCancelled('已取消')
    try:return operation({})
    except DownloadCancelled:raise
    except Exception as error:
        if cancel.is_set():raise DownloadCancelled('已取消')
        if getattr(error,'needs_browser',False):raise
        if not needs_login(error):raise
        errors=[str(error)]
    for auth in candidates(row,preferred):
        if cancel.is_set():raise DownloadCancelled('已取消')
        notify('平台要求登录，正在自动重试可用登录状态…')
        try:return operation(auth)
        except DownloadCancelled:raise
        except Exception as error:
            if cancel.is_set():raise DownloadCancelled('已取消')
            message=str(error).lower()
            if not needs_login(error) and not any(s in message for s in ('cookie','登录数据库','登录文件','dpapi','decrypt','keyring')):
                raise
            errors.append(str(error))
    raise LoginRequired('平台需要有效登录，自动重试未成功。请在浏览器登录后重试；仍不可用时可导入登录文件。\n\n'+ '\n\n'.join(errors)[-2500:])
