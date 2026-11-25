/* Execute the shipped extension worker with browser API fixtures. No account data. */
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const tabs = [{id: 1, active: false, url: 'https://www.youtube.com/'},
              {id: 2, active: true, url: 'https://www.youtube.com/watch?v=fixture'}];
let requested, lastQuery, scripts = 0;
let aigeiAllowed=false;
const aigeiLink={href:'https://www.aigei.com/item/rain.html'};
const aigeiBox={querySelectorAll:()=>[aigeiLink]};
const aigeiElement={closest:()=>aigeiBox};
let primaryNodes=[],canonicalNode=null;
const document={querySelectorAll:()=>primaryNodes,querySelector:()=>canonicalNode};
const aigeiPlayer={containerEl:{get:()=>aigeiElement},dbDuration:150000,allowPlaySecond:null,
  audioSound:{sound:{url:'https://s2.aigei.com/src/aud/mp3/rain.mp3?e=123&token=fixture',readyState:3,duration:150000}}};
const page = {ytcfg: {get: () => true}};
const location={origin:'https://www.youtube.com',href:'https://www.youtube.com/'};
const context = vm.createContext({URL, Map, JSON, Number, String, Promise, Error, AbortController,Array,
  setTimeout, clearTimeout, window: page, location,document,
  fetch: async (url, options) => {
    requested = {url, options};
    return {url: 'https://www.youtube.com' + url, ok: true, text: async () => url.startsWith('/results') ? '<html>fixture</html>' : '{"page":2}'};
  },
  chrome: {runtime: {id: 'fixture', onMessage: {addListener: () => {}}},
    permissions:{contains:async()=>aigeiAllowed},
    storage: {session: {get: async () => ({}), set: async () => {}, remove: async () => {}}},
    tabs: {query: async query => {lastQuery = query; return tabs.filter(tab=>query.url.some(pattern=>tab.url.startsWith(pattern.slice(0,-1))));}, get: async id => tabs.find(tab => tab.id === id)},
    scripting: {executeScript: async request => {scripts++; return [{result: await request.func(...(request.args || []))}];}}}
});
vm.runInContext(fs.readFileSync(path.join(root, 'browser-extension/worker.js'), 'utf8'), context);
async function run(code) { return vm.runInContext(code, context); }
(async () => {
  const address = 'ws://127.0.0.1:12345/browser?token=' + 'a'.repeat(43);
  assert.equal(await run(`validateAddress(${JSON.stringify(address)})`), address);
  for (const bad of ['wss://127.0.0.1:12345/browser?token=abc', 'ws://example.org:1234/browser?token='+'a'.repeat(43),
                     'ws://127.0.0.1:1234/other?token='+'a'.repeat(43)]) {
    assert.throws(() => vm.runInContext(`validateAddress(${JSON.stringify(bad)})`, context));
  }
  const found = await run("runJob({op:'discover'})");
  assert.deepEqual(JSON.parse(JSON.stringify(found)), [{id:2,signedIn:true},{id:1,signedIn:true}]);
  assert.deepEqual(JSON.parse(JSON.stringify(lastQuery)), {url: ['https://www.youtube.com/*']});
  page.ytcfg.get = () => false;
  assert.ok((await run("runJob({op:'discover'})")).every(tab => !tab.signedIn));
  await run("runJob({id:'search1',op:'search',tabId:1,query:'nature & music'})");
  assert.equal(requested.url, '/results?search_query=nature%20%26%20music&hl=zh-CN');
  assert.equal(requested.options.credentials, 'include');
  assert.equal(page.__resourceFinderRequests.size, 0);
  const continuation = await run("runJob({id:'next1',op:'continuation',tabId:1,body:{context:{client:{}},continuation:'cursor'}})");
  assert.equal(continuation.page, 2);
  assert.equal(requested.url, '/youtubei/v1/search');
  assert.equal(requested.options.method, 'POST');
  assert.equal(JSON.parse(requested.options.body).continuation, 'cursor');
  tabs[0].url = 'https://example.org/private';
  const before = scripts;
  await assert.rejects(run("runJob({op:'search',tabId:1,query:'test'})"), /YouTube/);
  assert.equal(scripts, before);
  await assert.rejects(run("runJob({op:'eval',tabId:2,code:'secret'})"), /不支持/);
  const manifest = JSON.parse(fs.readFileSync(path.join(root,'browser-extension/manifest.json')));
  assert.deepEqual(manifest.permissions, ['scripting','storage']);
  assert.ok(!manifest.host_permissions.includes('<all_urls>'));
  assert.deepEqual(manifest.optional_host_permissions,['https://www.aigei.com/*','https://aigei.com/*']);
  await assert.rejects(run("runJob({op:'aigei_media',pagePath:'/item/rain.html'})"),/启用爱给/);
  aigeiAllowed=true;tabs.push({id:3,active:true,url:'https://www.aigei.com/item/rain.html'});
  location.origin='https://www.aigei.com';location.href=tabs[2].url;page.AudioPlayerManager={playerArr:[aigeiPlayer]};
  const media=await run("runJob({op:'aigei_media',pagePath:'/item/rain.html'})");
  assert.equal(media.durationSeconds,150);assert.equal(media.playbackLimitSeconds,0);assert.equal(media.url,aigeiPlayer.audioSound.sound.url);
  assert.deepEqual(JSON.parse(JSON.stringify(lastQuery)),{url:['https://www.aigei.com/*','https://aigei.com/*']});
  aigeiPlayer.allowPlaySecond=10;
  assert.equal((await run("runJob({op:'aigei_media',pagePath:'/item/rain.html'})")).playbackLimitSeconds,10);
  assert.equal(aigeiPlayer.allowPlaySecond,10); // Never modify platform audition policy.
  aigeiLink.href='https://www.aigei.com/item/different.html';
  await assert.rejects(run("runJob({op:'aigei_media',pagePath:'/item/rain.html'})"),/目标资源/);
  aigeiLink.href='https://www.aigei.com.evil.test/item/rain.html';
  await assert.rejects(run("runJob({op:'aigei_media',pagePath:'/item/rain.html'})"),/目标资源/);
  aigeiLink.href='https://www.aigei.com/item/rain.html';
  page.AudioPlayerManager.playerArr.push(aigeiPlayer);
  await assert.rejects(run("runJob({op:'aigei_media',pagePath:'/item/rain.html'})"),/目标资源/);
  page.AudioPlayerManager.playerArr.pop();aigeiElement.closest=()=>null;primaryNodes=[aigeiElement];location.pathname='/item/rain.html';
  canonicalNode={href:'https://www.aigei.com/item/rain.html'};
  assert.equal((await run("runJob({op:'aigei_media',pagePath:'/item/rain.html'})")).url,aigeiPlayer.audioSound.sound.url);
  canonicalNode.href='https://www.aigei.com/item/other.html';
  await assert.rejects(run("runJob({op:'aigei_media',pagePath:'/item/rain.html'})"),/目标资源/);
  canonicalNode.href='https://www.aigei.com/item/rain.html';primaryNodes=[aigeiElement,{}];
  await assert.rejects(run("runJob({op:'aigei_media',pagePath:'/item/rain.html'})"),/目标资源/);
  await assert.rejects(run("runJob({op:'aigei_media',pagePath:'https://evil.test/data'})"),/路径无效/);
  assert.ok(!fs.readFileSync(path.join(root,'browser-extension/worker.js'),'utf8').includes('fileGet('));
  console.log('PASS extension: loopback pairing, all connected windows, sign-in detection, same-origin authenticated search/continuation, closed/foreign tab rejection, no arbitrary operations.');
  console.log('PASS Aigei extension: optional site permission, exact item/host, read-only loaded player, duration/limit preservation, ambiguous player and invalid path rejection.');
})().catch(error => { console.error(error); process.exitCode = 1; });
