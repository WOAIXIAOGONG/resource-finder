"""Metadata-only Aigei Streamable HTTP MCP and OAuth PKCE; no download tools."""
import base64,hashlib,json,os,secrets,threading,time
from http.server import BaseHTTPRequestHandler,HTTPServer
from pathlib import Path
from urllib.parse import urlencode,urlsplit,parse_qs
import requests
from .net import HttpClient
from .config import APP_VERSION

ORIGIN='https://www.aigei.com'
ENDPOINT=ORIGIN+'/mcp'
class MCPError(Exception):pass
class MCPRequired(MCPError):pass

def protect(value,decrypt=False):
    """Windows user-bound DPAPI. Never fall back to plaintext credentials."""
    import ctypes
    from ctypes import wintypes
    class Blob(ctypes.Structure):_fields_=[('size',wintypes.DWORD),('data',ctypes.POINTER(ctypes.c_char))]
    raw=ctypes.create_string_buffer(value);inp=Blob(len(value),raw);out=Blob()
    if os.name!='nt':raise MCPError('此版本的账号授权凭据需要 Windows 安全存储')
    fn=ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    if not fn(ctypes.byref(inp),None,None,None,None,1,ctypes.byref(out)):raise MCPError('无法读取安全凭据，请重新连接爱给账号')
    try:return ctypes.string_at(out.data,out.size)
    finally:ctypes.windll.kernel32.LocalFree(out.data)

class Transport:
    def __init__(self,settings,cancel=None):self.settings=settings;self.cancel=cancel or threading.Event()
    def request(self,method,url,**kwargs):
        if urlsplit(url).scheme!='https' or urlsplit(url).netloc!='www.aigei.com':raise MCPError('拒绝向非爱给官方地址发送授权请求')
        if self.cancel.is_set():raise MCPError('已取消')
        http=HttpClient(self.settings);http.cancel_event=self.cancel
        routes=http.routes_for(host='www.aigei.com');proxy=http.proxy_url() if routes and routes[0]=='proxy' else ''
        def operation():
            # Never retry POST implicitly or downgrade credential-bearing TLS.
            with requests.Session() as session:
                session.trust_env=False
                if proxy:session.proxies={'http':proxy,'https':proxy}
                with session.request(method,url,timeout=(20,180),allow_redirects=False,stream=True,**kwargs) as r:
                    if r.status_code==401:raise MCPRequired('请先连接爱给官方 MCP，或重新授权已失效的账号')
                    if r.status_code not in (200,201,202,204):raise MCPError('爱给官方接口 HTTP %d；请稍后重试'%r.status_code)
                    if r.status_code in (202,204):return {},dict(r.headers)
                    parts=[];size=0
                    if 'text/event-stream' in r.headers.get('Content-Type',''):
                        events=[];lines=[];expected=(kwargs.get('json') or {}).get('id')
                        for line in r.iter_lines(chunk_size=1):
                            if self.cancel.is_set():raise MCPError('已取消')
                            size+=len(line)+1
                            if size>4*1024*1024:raise MCPError('爱给接口响应过大')
                            if line.startswith(b'data:'):lines.append(line[5:].lstrip())
                            elif not line and lines:
                                try:event=json.loads(b'\n'.join(lines))
                                except ValueError:lines=[];continue
                                events.append(event);lines=[]
                                if isinstance(event,dict) and event.get('id')==expected:return events,dict(r.headers)
                        return events,dict(r.headers)
                    for part in r.iter_content(16384):
                        if self.cancel.is_set():raise MCPError('已取消')
                        size+=len(part)
                        if size>4*1024*1024:raise MCPError('爱给接口响应过大')
                        parts.append(part)
                    raw=b''.join(parts).decode('utf-8-sig')
                    try:return json.loads(raw),dict(r.headers)
                    except ValueError:raise MCPError('爱给接口未返回有效 JSON')
        try:return http._request_wait(operation,self.cancel,time.monotonic()+180)
        except (MCPError,MCPRequired):raise
        except Exception as error:
            if self.cancel.is_set():raise MCPError('已取消') from error
            # Do not log credential-bearing requests, response bodies or signed URLs.
            raise MCPError('爱给官方请求失败（%s）；请稍后检查网络并重试'%type(error).__name__) from error

class AigeiMCP:
    def __init__(self,settings,root):
        self.settings=settings;self.root=Path(root);self.path=self.root/'aigei-oauth.dat';self.lock=threading.RLock()
    def load(self):
        try:return json.loads(protect(self.path.read_bytes(),True))
        except FileNotFoundError:return {}
        except Exception:raise MCPRequired('爱给授权无法在当前 Windows 用户下读取，请重新连接')
    def save(self,data):
        self.root.mkdir(parents=True,exist_ok=True);tmp=self.path.with_suffix('.tmp')
        tmp.write_bytes(protect(json.dumps(data).encode()));os.replace(tmp,self.path)
    def connected(self):
        try:return bool(self.load().get('access_token'))
        except MCPRequired:return False
    def disconnect(self):
        with self.lock:self.path.unlink(missing_ok=True)
    def token(self,cancel):
        with self.lock:
            data=self.load()
            if not data.get('access_token'):raise MCPRequired('通过 MCP 查询素材信息需要先连接爱给账号，请完成一次浏览器授权')
            if data.get('expires_at',0)>time.time()+45:return data['access_token']
            if not data.get('refresh_token'):raise MCPRequired('爱给授权已过期，请重新连接账号')
            result,_=Transport(self.settings,cancel).request('POST',ORIGIN+'/open/oauth/token',data={
                'grant_type':'refresh_token','refresh_token':data['refresh_token'],'client_id':data['client_id'],'resource':ENDPOINT})
            if not result.get('access_token'):raise MCPRequired('爱给授权更新失败，请重新连接账号')
            data.update(result);data['expires_at']=time.time()+float(result.get('expires_in',3600));self.save(data);return data['access_token']
    def call(self,name,arguments,cancel=None):
        # This connector is metadata-only. Never enable a download or preview
        # download tool here: issuing those URLs can settle a platform fee.
        allowed={'audio-search','audio-detail','image-search','image-detail','video-search','video-detail'}
        if name not in allowed:raise MCPError('爱给 MCP 仅用于资源信息查询，此工具未开放')
        cancel=cancel or threading.Event();transport=Transport(self.settings,cancel)
        headers={'Accept':'application/json, text/event-stream','Authorization':'Bearer '+self.token(cancel)}
        def rpc(identity,method,params=None):
            body={'jsonrpc':'2.0','method':method}
            if identity is not None:body['id']=identity
            if params is not None:body['params']=params
            value,meta=transport.request('POST',ENDPOINT,json=body,headers=headers)
            session=next((v for k,v in meta.items() if k.lower()=='mcp-session-id'),None)
            if session:headers['Mcp-Session-Id']=session
            if identity is None:return {}
            if isinstance(value,list):value=next((v for v in value if isinstance(v,dict) and v.get('id')==identity),{})
            if not isinstance(value,dict) or value.get('id')!=identity:raise MCPError('爱给 MCP 响应编号不匹配')
            if value.get('error'):raise MCPError('爱给 MCP 协议错误 '+str(value['error'].get('code','')))
            return value.get('result',{})
        initialized=rpc(1,'initialize',{'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'ResourceFinder','version':APP_VERSION}})
        version=initialized.get('protocolVersion')
        if version not in ('2024-11-05','2025-03-26','2025-06-18','2025-11-25'):raise MCPError('爱给 MCP 返回了不支持的协议版本')
        headers['MCP-Protocol-Version']=version;rpc(None,'notifications/initialized')
        available=rpc(2,'tools/list').get('tools',[])
        if name not in [t.get('name') for t in available]:raise MCPError('爱给 MCP 未提供 '+name)
        value=rpc(3,'tools/call',{'name':name,'arguments':arguments})
        data=value.get('structuredContent')
        if not isinstance(data,dict):
            for part in value.get('content',[]):
                if part.get('type')=='text':
                    try:data=json.loads(part.get('text',''));break
                    except ValueError:pass
        if not isinstance(data,dict):raise MCPError('爱给 MCP 没有返回业务数据')
        if value.get('isError') or str(data.get('code'))!='10001':
            code=str(data.get('code','未知'));message=str(data.get('message','调用失败'))[:300]
            if code in ('50302',):raise MCPRequired('爱给账号授权已失效，请重新连接')
            raise MCPError('爱给官方返回 %s：%s'%(code,message))
        return data.get('data')

class OAuthFlow:
    """One explicit user-initiated flow. URL is displayed, never auto-opened."""
    def __init__(self,client,cancel=None):
        self.client=client;self.cancel=cancel or threading.Event();self.state=secrets.token_urlsafe(32)
        self.verifier=secrets.token_urlsafe(48);self.code='';self.server=None;self.redirect='';self.url='';self.error=''
    def prepare(self):
        flow=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def do_GET(self):
                p=urlsplit(self.path);params=parse_qs(p.query)
                if p.path!='/aigei/callback' or params.get('state')!=[flow.state]:self.send_error(400);return
                if flow.code or flow.error:self.send_error(409);return
                flow.code=params.get('code',[''])[0]
                if params.get('error') or not flow.code:flow.error='账号授权未完成，请返回工具重试'
                body=('授权已收到，请返回资源搜寻器。' if flow.code else flow.error).encode('utf-8')
                self.send_response(200);self.send_header('Content-Type','text/plain; charset=utf-8');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        class LoopbackServer(HTTPServer):
            def get_request(self):
                socket,address=super().get_request();socket.settimeout(2);return socket,address
        self.server=LoopbackServer(('127.0.0.1',0),Handler);self.server.timeout=.2
        self.redirect='http://127.0.0.1:%d/aigei/callback'%self.server.server_port
        try:
            transport=Transport(self.client.settings,self.cancel)
            meta,_=transport.request('GET',ORIGIN+'/.well-known/oauth-authorization-server')
            for key,path in [('authorization_endpoint','/open/oauth/authorize'),('token_endpoint','/open/oauth/token'),('registration_endpoint','/open/oauth/register')]:
                if meta.get(key)!=ORIGIN+path:raise MCPError('爱给授权端点与官方预期不符，已停止连接')
            registered,_=transport.request('POST',meta['registration_endpoint'],json={
                'client_name':'资源搜寻器','redirect_uris':[self.redirect],
                'grant_types':['authorization_code','refresh_token'],'response_types':['code'],'token_endpoint_auth_method':'none'})
            self.client_id=registered.get('client_id')
            if not self.client_id:raise MCPError('爱给未返回 OAuth 客户端编号')
            challenge=base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).rstrip(b'=').decode()
            self.url=meta['authorization_endpoint']+'?'+urlencode({'client_id':self.client_id,'redirect_uri':self.redirect,
                'response_type':'code','scope':'mcp.access','resource':ENDPOINT,'state':self.state,
                'code_challenge':challenge,'code_challenge_method':'S256'})
            return self.url
        except Exception:self.close();raise
    def finish(self):
        deadline=time.monotonic()+180
        try:
            while not self.code and not self.error:
                if self.cancel.is_set():raise MCPError('已取消连接')
                if time.monotonic()>deadline:raise MCPError('等待授权超过3分钟，请重新连接')
                self.server.handle_request()
            if self.error:raise MCPError(self.error)
            result,_=Transport(self.client.settings,self.cancel).request('POST',ORIGIN+'/open/oauth/token',data={
                'grant_type':'authorization_code','client_id':self.client_id,'code':self.code,'redirect_uri':self.redirect,
                'code_verifier':self.verifier,'resource':ENDPOINT})
            if not result.get('access_token') or str(result.get('token_type','')).lower()!='bearer':raise MCPError('爱给未返回有效授权')
            if self.cancel.is_set():raise MCPError('已取消连接')
            result.update(client_id=self.client_id,expires_at=time.time()+float(result.get('expires_in',3600)))
            with self.client.lock:self.client.save(result)
        finally:self.close()
    def close(self):
        if self.server:self.server.server_close();self.server=None
