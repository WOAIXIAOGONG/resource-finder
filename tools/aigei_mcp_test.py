import testenv
import sys,tempfile,threading,unittest,time,json
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import aigei_mcp as m
from app.config import Settings
import requests

class MCPTests(unittest.TestCase):
    def test_connector_allows_metadata_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            client=m.AigeiMCP(Settings(),tmp)
            with patch.object(m.Transport,'request') as network,patch.object(client,'token') as token:
                for name in ['audio-download','image-download','video-download','image-preview-download','video-preview-download']:
                    with self.assertRaises(m.MCPError):client.call(name,{'fileId':'anything'})
                network.assert_not_called();token.assert_not_called()
    def test_dpapi_isolation_and_disconnect(self):
        with tempfile.TemporaryDirectory() as tmp:
            a=m.AigeiMCP(Settings(),Path(tmp)/'a');b=m.AigeiMCP(Settings(),Path(tmp)/'b')
            a.save({'access_token':'private-token','expires_at':time.time()+999})
            self.assertNotIn(b'private-token',a.path.read_bytes());self.assertTrue(a.connected());self.assertFalse(b.connected())
            self.assertEqual(a.token(threading.Event()),'private-token');a.disconnect();self.assertFalse(a.connected())
    def test_streamable_handshake_and_business_errors(self):
        calls=[];business={'code':'10001','data':{'itemList':[]}}
        def request(_self,method,url,**kwargs):
            body=kwargs['json'];calls.append(body);identity=body.get('id');operation=body['method']
            if operation=='initialize':result={'protocolVersion':'2025-06-18'}
            elif operation=='tools/list':result={'tools':[{'name':'audio-search'}]}
            elif operation=='notifications/initialized':return {},{}
            else:result={'structuredContent':business}
            return [{'jsonrpc':'2.0','id':identity,'result':result}],{'mcp-session-id':'session'}
        with tempfile.TemporaryDirectory() as tmp:
            c=m.AigeiMCP(Settings(),tmp)
            with patch.object(c,'token',return_value='private'),patch.object(m.Transport,'request',request):
                self.assertEqual(c.call('audio-search',{'keyword':'雨声','cat':'sound'}),{'itemList':[]})
                self.assertEqual([p['method'] for p in calls],['initialize','notifications/initialized','tools/list','tools/call'])
                business['code']='50303'
                with self.assertRaises(m.MCPError):c.call('audio-search',{})
    def test_oauth_state_pkce_callback_and_storage(self):
        seen=[]
        def request(_self,method,url,**kwargs):
            seen.append((method,url,kwargs))
            if '/.well-known/' in url:return {'authorization_endpoint':m.ORIGIN+'/open/oauth/authorize','token_endpoint':m.ORIGIN+'/open/oauth/token','registration_endpoint':m.ORIGIN+'/open/oauth/register'},{}
            if url.endswith('/register'):return {'client_id':'registered-client'},{}
            return {'access_token':'secret','token_type':'Bearer','expires_in':3600,'refresh_token':'refresh'},{}
        with tempfile.TemporaryDirectory() as tmp,patch.object(m.Transport,'request',request):
            client=m.AigeiMCP(Settings(),tmp);flow=m.OAuthFlow(client);url=flow.prepare()
            from urllib.parse import parse_qs,urlsplit
            params=parse_qs(urlsplit(url).query);self.assertEqual(params['code_challenge_method'],['S256']);self.assertNotIn(flow.verifier,url)
            errors=[]
            def run():
                try:flow.finish()
                except Exception as e:errors.append(e)
            thread=threading.Thread(target=run);thread.start()
            wrong=requests.get(flow.redirect,params={'state':'wrong','code':'bad'},timeout=3);self.assertEqual(wrong.status_code,400);self.assertFalse(flow.code)
            ok=requests.get(flow.redirect,params={'state':flow.state,'code':'real-code'},timeout=3);self.assertEqual(ok.status_code,200)
            thread.join(4);self.assertFalse(thread.is_alive());self.assertFalse(errors);self.assertTrue(client.connected());self.assertIsNone(flow.server)
            form=seen[-1][2]['data'];self.assertEqual(form['code_verifier'],flow.verifier);self.assertEqual(form['resource'],m.ENDPOINT)
    def test_cancelled_oauth_closes_port(self):
        def request(_self,method,url,**kwargs):
            if method=='GET':return {'authorization_endpoint':m.ORIGIN+'/open/oauth/authorize','token_endpoint':m.ORIGIN+'/open/oauth/token','registration_endpoint':m.ORIGIN+'/open/oauth/register'},{}
            return {'client_id':'registered'},{}
        with tempfile.TemporaryDirectory() as tmp,patch.object(m.Transport,'request',request):
            c=m.AigeiMCP(Settings(),tmp);flow=m.OAuthFlow(c);flow.prepare();flow.cancel.set()
            with self.assertRaises(m.MCPError):flow.finish()
            self.assertIsNone(flow.server);self.assertFalse(c.connected())
    def test_refresh_rotates_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            c=m.AigeiMCP(Settings(),tmp);c.save({'client_id':'client','access_token':'old','refresh_token':'r1','expires_at':0})
            with patch.object(m.Transport,'request',return_value=({'access_token':'new','refresh_token':'r2','expires_in':3600},{})) as request:
                self.assertEqual(c.token(threading.Event()),'new');self.assertEqual(c.load()['refresh_token'],'r2');request.assert_called_once()
    def test_transport_has_no_retry_on_ambiguous_download_failure(self):
        with patch.object(requests.Session,'request',side_effect=requests.Timeout()) as request:
            with self.assertRaises(m.MCPError):m.Transport(Settings(proxy_mode='direct')).request('POST',m.ENDPOINT,json={'method':'tools/call'})
            self.assertEqual(request.call_count,1)
        with self.assertRaises(m.MCPError):m.Transport(Settings()).request('POST','https://aigei.com.evil.test/mcp')
    def test_transport_parses_sse_and_never_follows_redirects(self):
        class Response:
            status_code=200;headers={'Content-Type':'text/event-stream'}
            def __enter__(self):return self
            def __exit__(self,*_):pass
            def iter_lines(self,**_):
                yield from [b'event: message',b'data: {"jsonrpc":"2.0",',b'data: "id":3,"result":{}}',b'']
                raise AssertionError('Must stop at the response, not wait for an open SSE stream to end')
        with patch.object(requests.Session,'request',return_value=Response()) as request:
            value,_=m.Transport(Settings(proxy_mode='direct')).request('POST',m.ENDPOINT,json={'id':3})
            self.assertEqual(value[0]['id'],3);self.assertFalse(request.call_args.kwargs['allow_redirects'])
            self.assertNotIn('verify',request.call_args.kwargs)

if __name__=='__main__':unittest.main(verbosity=2)
