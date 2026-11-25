/* Exercise shipped auto-pair worker and content script, no browser/account data. */
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),path=require('node:path');
const root=path.resolve(__dirname,'..');let handler,created=[];
class Socket{
  static CONNECTING=0;static OPEN=1;
  constructor(url){this.url=url;this.readyState=0;created.push(this);}
  close(){this.readyState=3;this.onclose?.();}send(){}
}
const context=vm.createContext({URL,Map,JSON,Number,String,Promise,Error,WebSocket:Socket,setTimeout,clearTimeout,
 chrome:{runtime:{id:'fixture',onMessage:{addListener(fn){handler=fn;}}},
 storage:{session:{get:async()=>({}),set:async()=>{},remove:async()=>{}}}}});
vm.runInContext(fs.readFileSync(path.join(root,'browser-extension/worker.js'),'utf8'),context);
const nonce='b'.repeat(43),address='ws://127.0.0.1:12345/browser?token='+'a'.repeat(43);
const sender={id:'fixture',frameId:0,tab:{id:1},url:'http://127.0.0.1:4567/rf-connect/'+nonce};
const dispatch=(msg,who)=>new Promise(resolve=>{if(!handler(msg,who,resolve))resolve(null);});
(async()=>{
 for(const who of [{...sender,url:'https://evil.test/rf-connect/'+nonce},
    {...sender,url:'http://127.0.0.1:4567/wrong'}, {...sender,frameId:1},
    {...sender,id:'intruder'}, {...sender,url:sender.url+'?redirect=evil'}]){
   assert.equal(await dispatch({action:'auto_pair',address},who),null);
 }
 assert.equal(created.length,0);
 assert.equal(await dispatch({action:'connect',address},sender),null);
 const accepted=await dispatch({action:'auto_pair',address},sender);
 assert.equal(accepted.accepted,true);assert.equal(created.length,1);assert.equal(created[0].url,address);
 await dispatch({action:'auto_pair',address},sender);assert.equal(created.length,1,'duplicate content script must not replace live socket');
 const bad=await dispatch({action:'auto_pair',address:address.replace('127.0.0.1','evil.test')},sender);
 assert.ok(!bad.accepted);assert.equal(created.length,1);
 const code=fs.readFileSync(path.join(root,'browser-extension/pairing.js'),'utf8');
 async function content(overrides={}){
   let sent=[],attrs={};const win={};win.top=win;
   const location={protocol:'http:',hostname:'127.0.0.1',port:'4567',pathname:'/rf-connect/'+nonce,search:'',hash:'',...overrides};
   vm.runInNewContext(code,{window:win,location,document:{documentElement:{getAttribute:()=>address,setAttribute:(k,v)=>{attrs[k]=v;}}},
     chrome:{runtime:{sendMessage:async value=>{sent.push(value);return {accepted:true};}}}});
   await Promise.resolve();return {sent,attrs};
 }
 assert.equal((await content()).sent.length,1);
 assert.equal((await content()).attrs['data-rf-extension'],'connected');
 for(const wrong of [{hostname:'evil.test'},{protocol:'https:'},{pathname:'/other'},{search:'?token=untrusted'},{hash:'#spoof'}]){
   assert.equal((await content(wrong)).sent.length,0);
 }
 const manifest=JSON.parse(fs.readFileSync(path.join(root,'browser-extension/manifest.json'),'utf8'));
 assert.deepEqual(manifest.content_scripts[0].matches,['http://127.0.0.1/*']);assert.equal(manifest.content_scripts[0].all_frames,false);
 assert.ok(!manifest.permissions.includes('cookies'));assert.ok(!manifest.permissions.includes('management'));
 console.log('PASS automatic pairing: exact local page + top frame + extension sender, remote/iframe/manual-action rejection, duplicate socket reuse, bounded content access; no cookie or installation permissions.');
})().catch(error=>{console.error(error);process.exitCode=1;});
