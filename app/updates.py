"""GitHub release updates: authenticated download, verified staging, reversible install."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from urllib.parse import urlsplit
import requests
from .config import APP_VERSION, app_dir
from .net import detect_proxy
from .update_channel import REPOSITORY

EXE = '资源搜寻器.exe'
MANIFEST = 'update-manifest.json'
MANAGED = {EXE, '_internal', 'app', 'tools', 'assets', 'browser-extension', 'download-login-helper',
           'vendor', 'main.py', 'requirements.txt', 'README.md', 'AGENTS.md', 'CHANGELOG.md',
           '使用说明.txt', '详情介绍.md', '.gitignore', MANIFEST}
MAX_ZIP = 1024**3
MAX_EXPANDED = 3*1024**3

class UpdateError(Exception): pass
class UpdateCancelled(UpdateError): pass
class UpdateHTTPError(UpdateError):
    def __init__(self,message,status,kind='http'):
        super().__init__(message);self.status=status;self.kind=kind

def version(value):
    match = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)', str(value))
    if not match: raise UpdateError('版本号无效，仅支持正式版本 x.y.z')
    return tuple(map(int, match.groups()))

def sha(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024), b''): digest.update(chunk)
    return digest.hexdigest()

def atomic_json(path, data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        temp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        os.replace(temp,path)
    finally:
        temp.unlink(missing_ok=True)

def local_home(install=None):
    override=os.environ.get('RESOURCE_FINDER_HOME')
    base=Path(override) if override else Path(os.environ.get('LOCALAPPDATA',tempfile.gettempdir()))/'ResourceFinder'
    key=hashlib.sha256(str(Path(install or app_dir()).resolve()).casefold().encode()).hexdigest()[:20]
    return base/'updates'/key

class UpdateSettings:
    def __init__(self, install=None):
        self.home=local_home(install);self.path=self.home/'settings.json'
        try:self.data=json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError,ValueError):self.data={}
        if not isinstance(self.data,dict):self.data={}
    @property
    def repository(self):return REPOSITORY or str(self.data.get('repository',''))
    @property
    def automatic(self):return self.data.get('automatic',True) is not False
    def token(self):
        import base64
        value=self.data.get('credential','')
        if not value:return ''
        try:
            from .aigei_mcp import protect
            return protect(base64.b64decode(value),decrypt=True).decode('utf-8')
        except Exception:raise UpdateError('本机授权无法读取，请重新填写只读访问令牌') from None
    def save(self, *, token=None, automatic=None, repository=None):
        import base64
        data=dict(self.data)
        if token is not None:
            if token:
                from .aigei_mcp import protect
                try:data['credential']=base64.b64encode(protect(token.strip().encode())).decode('ascii')
                except Exception:raise UpdateError('Windows 安全存储失败，未保存授权') from None
            else:data.pop('credential',None)
        if automatic is not None:data['automatic']=bool(automatic)
        if repository is not None:data['repository']=repository.strip()
        atomic_json(self.path,data);self.data=data

class ReleaseClient:
    _api_cooldowns={}
    def __init__(self, settings, repository, token='', cancel=None):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repository):raise UpdateError('尚未配置有效的 GitHub 更新仓库')
        self.repository=repository;self.token=token;self.cancel=cancel or threading.Event()
        self.session=requests.Session();self.session.trust_env=False
        proxy=detect_proxy(settings)
        self._route=proxy or 'direct'
        if proxy:self.session.proxies={'http':proxy,'https':proxy}
    def close(self):self.session.close()
    def check_cancel(self):
        if self.cancel.is_set():raise UpdateCancelled('已取消更新')
    def request(self, url, binary=False):
        # Credential is attached ONLY to api.github.com, never to a redirect host.
        for _ in range(5):
            self.check_cancel();parts=urlsplit(url)
            if parts.scheme!='https' or parts.username or parts.password or parts.port not in (None,443):raise UpdateError('更新地址不安全')
            host=parts.hostname
            if host not in ('github.com','api.github.com','release-assets.githubusercontent.com','objects.githubusercontent.com'):
                raise UpdateError('拒绝非 GitHub 官方下载地址')
            if host=='api.github.com' and self._api_cooldowns.get((self._route,bool(self.token)),0)>time.time():
                raise UpdateHTTPError('GitHub API 请求额度暂时受限，请稍后再试；这不代表仓库已转为私有。',403,'rate_limit')
            headers={'Accept':'application/octet-stream' if binary else 'application/vnd.github+json',
                     'User-Agent':'ResourceFinder/'+APP_VERSION,'X-GitHub-Api-Version':'2022-11-28'}
            if host=='api.github.com' and self.token:headers['Authorization']='Bearer '+self.token
            try:r=self.session.get(url,headers=headers,allow_redirects=False,stream=True,timeout=(10,30))
            except requests.RequestException:raise UpdateError('无法连接 GitHub，请检查网络或代理后重试') from None
            if r.status_code in (301,302,303,307,308):
                url=r.headers.get('Location','');r.close();continue
            if r.status_code!=200:
                code=r.status_code;headers=r.headers;body=b''
                try:
                    for block in r.iter_content(1024):
                        body+=block
                        if len(body)>=4096:break
                except requests.RequestException:pass
                finally:r.close()
                limited=code==429 or headers.get('X-RateLimit-Remaining')=='0' or b'rate limit' in body.lower()
                if limited:
                    until=time.time()+60
                    for header,relative in [('Retry-After',True),('X-RateLimit-Reset',False)]:
                        try:
                            value=float(headers.get(header,''));until=max(until,time.time()+value if relative else value)
                        except (ValueError,TypeError):pass
                    if host=='api.github.com':self._api_cooldowns[(self._route,bool(self.token))]=until
                    wait=max(1,int((until-time.time()+59)//60))
                    raise UpdateHTTPError('GitHub 请求额度暂时受限（HTTP %d），请约 %d 分钟后重试；共享代理出口也可能触发限流，并非仓库私有。'%(code,wait),code,'rate_limit')
                if code==404:raise UpdateHTTPError('更新源目前无法公开访问（HTTP 404）：可能已转为私有、被移除，或尚无公开发布。暂时无法更新，当前版本仍可使用。',code,'unavailable')
                if code==401:raise UpdateHTTPError('GitHub 拒绝了请求授权（HTTP 401），暂时无法检查更新。',code,'authorization')
                if code==403:raise UpdateHTTPError('GitHub 拒绝了更新请求（HTTP 403），可能是访问策略或代理出口限制；尚不能判断仓库可见性。可在浏览器打开下载页核查。',code,'forbidden')
                raise UpdateHTTPError('GitHub 请求失败（HTTP %d），暂时无法检查更新。'%code,code)
            return r
        raise UpdateError('GitHub 下载重定向次数过多')
    def read_json(self,url):
        with self.request(url) as r:
            chunks=[];size=0
            for chunk in r.iter_content(65536):
                self.check_cancel();size+=len(chunk)
                if size>2*1024*1024:raise UpdateError('版本信息过大')
                chunks.append(chunk)
        try:data=json.loads(b''.join(chunks))
        except ValueError:raise UpdateError('GitHub 版本信息无法解析') from None
        if not isinstance(data,dict):raise UpdateError('GitHub 版本信息格式无效')
        return data
    def public_latest(self,current):
        data=self.read_json('https://github.com/'+self.repository+'/releases/latest/download/update-info.json')
        if data.get('schema')!=1 or data.get('repository')!=self.repository:raise UpdateError('公开更新清单与当前仓库不一致')
        ver='.'.join(map(str,version(data.get('version'))))
        name='resource-finder-v%s-win64.zip'%ver
        expected='https://github.com/%s/releases/download/v%s/%s'%(self.repository,ver,name)
        if data.get('name')!=name or data.get('url')!=expected:raise UpdateError('公开更新清单的下载地址无效')
        if not re.fullmatch('[0-9a-f]{64}',str(data.get('sha256',''))):raise UpdateError('公开更新清单缺少 SHA256 校验')
        if type(data.get('size')) is not int or not 0<data['size']<=MAX_ZIP:raise UpdateError('公开更新包大小无效')
        if version(ver)<=version(current):return None
        return dict(version=ver,name=name,url=expected,size=data['size'],sha256=data['sha256'],notes=str(data.get('notes') or '')[:12000],channel='public')
    def latest(self, current=APP_VERSION):
        try:return self.public_latest(current)
        except UpdateCancelled:raise
        except UpdateError as public_error:
            # Older releases may lack the public manifest. Consult API once;
            # rate-limited API calls are held until their server-specified reset.
            try:return self.api_latest(current)
            except UpdateCancelled:raise
            except UpdateHTTPError as api_error:
                if api_error.kind=='unavailable' and not (isinstance(public_error,UpdateHTTPError) and public_error.kind=='unavailable'):
                    raise public_error from None
                raise api_error from None
    def api_latest(self, current=APP_VERSION):
        data=self.read_json('https://api.github.com/repos/'+self.repository+'/releases/latest')
        if data.get('draft') or data.get('prerelease'):raise UpdateError('更新源不是正式发布')
        tag=data.get('tag_name','');parsed=version(tag)
        if parsed<=version(current):return None
        ver='.'.join(map(str,parsed));name='resource-finder-v%s-win64.zip'%ver
        assets=[a for a in data.get('assets',[]) if a.get('name')==name and a.get('state')=='uploaded']
        if len(assets)!=1:raise UpdateError('新版尚未提供完整的 Windows 更新包')
        asset=assets[0];digest=asset.get('digest','')
        if not re.fullmatch(r'sha256:[0-9a-fA-F]{64}',digest):raise UpdateError('更新包缺少 GitHub SHA256 校验，拒绝安装')
        if not isinstance(asset.get('id'),int) or not 0<asset.get('size',0)<=MAX_ZIP:raise UpdateError('更新包大小或编号无效')
        return dict(version=ver,notes=str(data.get('body') or '')[:12000],asset_id=asset['id'],
                    size=asset['size'],sha256=digest[7:].lower(),name=name)
    def download(self, release, destination, progress=lambda *_:None):
        destination=Path(destination);count=0;digest=hashlib.sha256()
        try:
            if release.get('channel')=='public':
                expected='https://github.com/%s/releases/download/v%s/resource-finder-v%s-win64.zip'%(self.repository,release['version'],release['version'])
                if release.get('url')!=expected:raise UpdateError('公开更新下载地址校验失败')
                url=expected
            else:url='https://api.github.com/repos/%s/releases/assets/%d'%(self.repository,release['asset_id'])
            with self.request(url,True) as r,destination.open('xb') as f:
                for chunk in r.iter_content(256*1024):
                    self.check_cancel()
                    if not chunk:continue
                    count+=len(chunk)
                    if count>release['size'] or count>MAX_ZIP:raise UpdateError('更新包长度不符')
                    f.write(chunk);digest.update(chunk);progress(count,release['size'])
            if count!=release['size'] or digest.hexdigest()!=release['sha256']:raise UpdateError('更新包 SHA256 校验失败，未修改现有程序')
        except BaseException:
            destination.unlink(missing_ok=True);raise

def safe_member(name):
    if '\\' in name or ':' in name or '\x00' in name:raise UpdateError('更新包路径无效')
    p=PurePosixPath(name)
    if p.is_absolute() or not p.parts or any(part in ('.','..') or part.endswith((' ','.')) for part in p.parts):raise UpdateError('更新包包含越界路径')
    for part in p.parts:
        if re.fullmatch(r'(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?',part):raise UpdateError('更新包包含保留设备名')
    return p

def write_manifest(directory, ver):
    directory=Path(directory);version(ver)
    files={}
    for path in sorted(directory.rglob('*')):
        if path.is_symlink():raise UpdateError('发布目录不能包含符号链接')
        if path.is_file() and path.name!=MANIFEST:
            relative=path.relative_to(directory).as_posix()
            if safe_member(relative).parts[0] not in MANAGED:raise UpdateError('发布目录含非程序文件：'+relative)
            files[relative]={'sha256':sha(path),'size':path.stat().st_size}
    atomic_json(directory/MANIFEST,dict(schema=1,version=ver,files=files))

def verify_stage(directory, expected, allow_user_data=False):
    directory=Path(directory)
    try:manifest=json.loads((directory/MANIFEST).read_text(encoding='utf-8'))
    except (OSError,ValueError):raise UpdateError('更新包缺少有效文件清单') from None
    if manifest.get('schema')!=1 or manifest.get('version')!=expected:raise UpdateError('更新包版本与发布版本不一致')
    files=manifest.get('files',{});actual=set();seen=set()
    if not isinstance(files,dict) or EXE not in files or not any(n.startswith('_internal/') for n in files):raise UpdateError('更新包缺少程序或运行环境')
    paths=[]
    for top in directory.iterdir():
        if allow_user_data and top.name not in MANAGED:continue
        paths.append(top)
        if top.is_dir():paths.extend(top.rglob('*'))
    for p in paths:
        if p.is_symlink() or getattr(p.stat(),'st_file_attributes',0)&0x400:raise UpdateError('更新目录含链接')
        if p.is_file():actual.add(p.relative_to(directory).as_posix())
    if actual!=set(files)|{MANIFEST}:raise UpdateError('更新包文件与清单不一致')
    for name,record in files.items():
        parts=safe_member(name).parts
        if parts[0] not in MANAGED or name.casefold() in seen:raise UpdateError('更新包包含不允许替换的路径')
        seen.add(name.casefold());path=directory.joinpath(*parts)
        if path.stat().st_size!=record.get('size') or sha(path)!=record.get('sha256'):raise UpdateError('更新文件校验失败：'+name)
    return manifest

def stage_archive(archive, directory, expected, cancel=None):
    directory=Path(directory);directory.mkdir(exist_ok=False)
    seen=set();total=0
    with zipfile.ZipFile(archive) as z:
        if len(z.infolist())>20000:raise UpdateError('更新包文件过多')
        for item in z.infolist():
            if cancel and cancel.is_set():raise UpdateCancelled('已取消更新')
            parts=safe_member(item.filename).parts
            if parts[0]!='资源搜寻器' or len(parts)<2:raise UpdateError('更新包根目录无效')
            relative=PurePosixPath(*parts[1:]);key=str(relative).casefold()
            if relative.parts[0] not in MANAGED or key in seen:raise UpdateError('更新包含存档、重复或未知路径')
            seen.add(key);total+=item.file_size
            if total>MAX_EXPANDED or stat.S_ISLNK(item.external_attr>>16) or item.flag_bits&1:raise UpdateError('更新包体积或文件类型不安全')
            target=directory.joinpath(*relative.parts)
            if item.is_dir():target.mkdir(parents=True,exist_ok=True);continue
            target.parent.mkdir(parents=True,exist_ok=True)
            with z.open(item) as src,target.open('xb') as dst:shutil.copyfileobj(src,dst,1024*1024)
    verify_stage(directory,expected)
    return directory

def prepare_update(settings, release, token, repository, install=None, cancel=None, progress=lambda *_:None):
    target=Path(install or app_dir()).resolve()
    if not (target/EXE).is_file():raise UpdateError('请在正式成品目录中更新')
    # Sibling staging ensures replacements and rollback stay on the same volume.
    work=Path(tempfile.mkdtemp(prefix='.resourcefinder-update-',dir=target.parent))
    if os.name=='nt':
        import ctypes
        ctypes.windll.kernel32.SetFileAttributesW(str(work),2)
    client=ReleaseClient(settings,repository,token,cancel)
    try:
        archive=work/'download.zip';client.download(release,archive,progress)
        stage=stage_archive(archive,work/'stage',release['version'],cancel)
        archive.unlink()
        from .agent_liveness import process_identity
        plan=dict(schema=1,target=str(target),stage=str(stage),version=release['version'],
                  parent=list(process_identity(os.getpid()) or ()),restart=True,receipt=str(local_home(target)/'last-update.json'))
        atomic_json(work/'plan.json',plan)
        return work/'plan.json'
    except BaseException:
        shutil.rmtree(work,ignore_errors=True);raise
    finally:client.close()

def apply_files(target, stage, backup, expected, healthcheck, progress=lambda *_:None):
    """Replace only managed roots. Record renames so failure can reverse them."""
    target=Path(target).resolve();stage=Path(stage).resolve();backup=Path(backup).resolve()
    if target==stage or target in stage.parents or stage in target.parents:raise UpdateError('更新目录不能与安装目录嵌套')
    verify_stage(stage,expected)
    def copy_file(src,dst):
        # Stream explicitly instead of Windows CopyFile2: DLL copies can stall
        # under file-system filters while the updater is itself a frozen EXE.
        progress('copying',Path(src).relative_to(stage).as_posix())
        with open(src,'rb') as source,open(dst,'xb') as output:
            shutil.copyfileobj(source,output,1024*1024)
        shutil.copystat(src,dst)
        return dst
    backup.mkdir(exist_ok=False);moved=[];installed=[]
    names=sorted({p.name for p in stage.iterdir()})
    for name in names:
        path=target/name
        if path.is_symlink() or (path.exists() and getattr(path.stat(),'st_file_attributes',0)&0x400):raise UpdateError('安装目录存在链接，停止更新')
    try:
        for name in names:
            progress('replacing',name)
            old=target/name
            if old.exists():os.replace(old,backup/name);moved.append(name)
            # The updater itself executes from stage, so COPY instead of moving its files.
            src=stage/name
            installed.append(name)
            if src.is_dir():shutil.copytree(src,old,copy_function=copy_file)
            else:copy_file(src,old)
        progress('checking','')
        healthcheck(target)
        progress('verified','')
    except BaseException:
        for name in reversed(installed):
            path=target/name
            if path.is_dir():shutil.rmtree(path)
            else:path.unlink(missing_ok=True)
        for name in reversed(moved):os.replace(backup/name,target/name)
        raise

def launch_update(plan):
    data=json.loads(Path(plan).read_text(encoding='utf-8'))
    if os.name!='nt':raise UpdateError('自动更新仅支持 Windows 成品版')
    shell=Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
    return subprocess.Popen([str(shell),'-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass',
                            '-File',str(Path(data['stage'])/'assets/update-install.ps1'),'-PlanPath',str(plan)],
                            creationflags=subprocess.CREATE_NO_WINDOW,close_fds=True,
                            env=dict(os.environ,PYINSTALLER_RESET_ENVIRONMENT='1'))
