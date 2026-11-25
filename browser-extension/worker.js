/* Fixed YouTube operations and read-only Aigei playback inspection. No cookie export. */
let socket = null;
let address = '';
let statusMessage = '尚未连接；请在工具中点击“已登录”，自动连接浏览器。';
const running = new Map();

function validateAddress(value) {
  const url = new URL(value);
  if (url.protocol !== 'ws:' || url.hostname !== '127.0.0.1' || !url.port ||
      url.pathname !== '/browser' || url.username || url.password || url.hash ||
      !/^[A-Za-z0-9_-]{32,100}$/.test(url.searchParams.get('token') || '')) {
    throw new Error('请使用资源搜寻器提供的本机连接信息。');
  }
  return url.href;
}

function connect(value) {
  address = validateAddress(value);
  if (socket) { const old = socket; socket = null; old.close(); }
  const ws = socket = new WebSocket(address);
  statusMessage = '正在连接工具…';
  ws.onmessage = async event => {
    let msg;
    try { msg = JSON.parse(event.data); } catch { return; }
    if (msg.type === 'connected') { statusMessage = '已连接，可回到工具继续搜索或重新解析资源。'; return; }
    if (msg.type === 'ping') { ws.send('{"type":"pong"}'); return; }
    if (msg.type === 'cancel') { await cancelJob(msg.id); return; }
    if (msg.type !== 'job' || typeof msg.id !== 'string') return;
    let result;
    try {
      if (Number.isInteger(msg.tabId)) running.set(msg.id, msg.tabId);
      result = {type: 'result', id: msg.id, ok: true, data: await runJob(msg)};
    }
    catch (error) { result = {type: 'result', id: msg.id, ok: false, error: String(error.message || error).slice(0, 900)}; }
    finally { running.delete(msg.id); }
    if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(result));
  };
  ws.onclose = () => {
    if (socket !== ws) return;
    socket = null;
    for (const id of running.keys()) cancelJob(id);
    statusMessage = '工具连接已断开；请回到工具点击“已登录”重新自动连接。';
    // No endless reconnects to a closed application; user explicitly reconnects.
    address = '';
    chrome.storage.session.remove('pair');
  };
  ws.onerror = () => { statusMessage = '无法连接，请确认工具仍打开，再点击“已登录”。'; };
}

async function cancelJob(id) {
  const tabId = running.get(id);
  if (!Number.isInteger(tabId)) return;
  try {
    await chrome.scripting.executeScript({target: {tabId}, world: 'MAIN', args: [id],
      func: key => window.__resourceFinderRequests?.get(key)?.abort()});
  } catch { /* tab already closed */ }
}

async function runJob(msg) {
  if (msg.op === 'aigei_media') return readAigeiMedia(msg);
  if (msg.op === 'discover') {
    const tabs = await chrome.tabs.query({url: ['https://www.youtube.com/*']});
    const ordered = tabs.sort((a, b) => Number(b.active) - Number(a.active));
    return Promise.all(ordered.map(async tab => {
      try {
        const response = await chrome.scripting.executeScript({target: {tabId: tab.id}, world: 'MAIN', func: () => {
          const cfg = window.ytcfg;
          return location.origin === 'https://www.youtube.com' &&
            (cfg?.get?.('LOGGED_IN') === true || cfg?.data_?.LOGGED_IN === true);
        }});
        return {id: tab.id, signedIn: response[0]?.result === true};
      } catch { return {id: tab.id, signedIn: false}; }
    }));
  }
  if (!['search', 'continuation'].includes(msg.op) || !Number.isInteger(msg.tabId)) throw new Error('不支持的浏览器操作');
  const tab = await chrome.tabs.get(msg.tabId);
  if (!tab.url || new URL(tab.url).origin !== 'https://www.youtube.com') throw new Error('YouTube 标签页已关闭或离开，请重新打开并验证登录。');
  if (msg.op === 'search' && (typeof msg.query !== 'string' || msg.query.length > 300)) throw new Error('搜索词无效');
  if (msg.op === 'continuation' && (typeof msg.body?.continuation !== 'string' || !msg.body?.context)) throw new Error('分页参数无效');
  const timeout = Math.min(600000, Math.max(3000, Number(msg.timeoutMs) || 180000));
  const response = await chrome.scripting.executeScript({target: {tabId: msg.tabId}, world: 'MAIN',
    args: [msg.op, msg.query || '', msg.body || null, timeout, msg.id],
    func: async (op, query, body, timeoutMs, jobId) => {
      if (location.origin !== 'https://www.youtube.com') throw new Error('请保持 YouTube 页面打开');
      const controller = new AbortController();
      const requests = window.__resourceFinderRequests ||= new Map();
      requests.set(jobId, controller);
      const timer = setTimeout(() => controller.abort(), timeoutMs);
      try {
        const url = op === 'search' ? '/results?search_query=' + encodeURIComponent(query) + '&hl=zh-CN' : '/youtubei/v1/search';
        const options = {credentials: 'include', signal: controller.signal};
        if (op === 'continuation') {
          options.method = 'POST';
          options.headers = {'Content-Type': 'application/json'};
          options.body = JSON.stringify({context: body.context, continuation: body.continuation});
        }
        const result = await fetch(url, options);
        if (new URL(result.url).origin !== location.origin) throw new Error('平台要求重新登录，请在浏览器中完成后再验证');
        if (!result.ok) throw new Error('YouTube 请求失败：HTTP ' + result.status);
        const text = await result.text();
        if (text.length > 6000000) throw new Error('YouTube 页面过大，请重试');
        return op === 'search' ? {html: text} : JSON.parse(text);
      } catch (error) { return {__rf_error: String(error.message || error)}; }
      finally { clearTimeout(timer); requests.delete(jobId); }
    }});
  const data = response[0]?.result;
  if (!data || data.__rf_error) throw new Error(data?.__rf_error || '页面无响应，请刷新 YouTube 页面后重试');
  return data;
}

async function readAigeiMedia(msg) {
  if (typeof msg.pagePath !== 'string' || !/^\/item\/[A-Za-z0-9_-]+\.html$/.test(msg.pagePath)) throw new Error('爱给资源路径无效');
  const origins = ['https://www.aigei.com/*', 'https://aigei.com/*'];
  if (!await chrome.permissions.contains({origins})) throw new Error('请先在浏览器扩展中点击“启用爱给播放提取”，允许读取该站点。');
  const tabs = (await chrome.tabs.query({url: origins})).sort((a,b) => Number(b.active)-Number(a.active));
  for (const tab of tabs) {
    try {
      const result = await chrome.scripting.executeScript({target:{tabId:tab.id},world:'MAIN',args:[msg.pagePath],
        func: inspectAigeiPage});
      const data=result[0]?.result;
      if (data?.state==='ready' && data.pagePath===msg.pagePath && typeof data.url==='string' && data.url.length<4096) return data;
    } catch { /* Closed/replaced tabs must not fall back to an unrelated player. */ }
  }
  throw new Error('未识别到目标资源的播放地址。请打开这条资源，在网页正常播放一次，再回到工具重新解析。');
}

/* RF_AIGEI_PLAYER_BEGIN */
function inspectAigeiPage(pagePath) {
  const origins=['https://www.aigei.com','https://aigei.com'];
  if (!origins.includes(location.origin)) return {state:'foreign'};
  if (/banIp|captcha|verify/i.test(location.pathname)) return {state:'verification'};
  const players=window.AudioPlayerManager?.playerArr;
  if (!Array.isArray(players)) return {state:'loading'};
  const primary=Array.from(document.querySelectorAll('.js-audio-container.audio-item-box-search-mode-false'));
  const declared=document.querySelector('link[rel="canonical"][href],meta[property="og:url"][content]');
  let declaredPath='';
  try {
    if (declared) {
      const u=new URL(declared.href||declared.content,location.href);
      declaredPath=origins.includes(u.origin)?u.pathname:'foreign';
    }
  } catch { declaredPath='foreign'; }
  const detail=location.pathname===pagePath && (!declaredPath||declaredPath===pagePath) && primary.length===1;
  const matches=[];
  for (const player of players.slice(0,1000)) {
    const element=player?.containerEl?.get?.(0);
    const box=element?.closest?.('.unit-container');
    const linked=box && Array.from(box.querySelectorAll('.item-view-url a[href]')).some(a=>{
      try {const u=new URL(a.href,location.href);return origins.includes(u.origin)&&u.pathname===pagePath;}
      catch {return false;}
    });
    if (!linked && !(detail&&element===primary[0])) continue;
    const sound=player.audioSound?.sound;
    const duration=Number(player.dbDuration)/1000 || Number(sound?.duration)/1000;
    const limit=Number(player.allowPlaySecond);
    const button=element.querySelector?.('.audio-player-btn');
    const rect=button?.getBoundingClientRect?.();
    const hit=rect&&document.elementFromPoint?.(rect.x+rect.width/2,rect.y+rect.height/2);
    const loaded=typeof sound?.url==='string' && sound.url.length>0 && sound.readyState;
    matches.push({state:loaded?'ready':'play',pagePath,url:loaded?sound.url:'',
      durationSeconds:Number.isFinite(duration)&&duration>0?duration:0,
      playbackLimitSeconds:Number.isFinite(limit)&&limit>0?limit:0,
      itemId:String(player.id||''),playing:sound?.playState===1&&!sound.paused,
      requestStatus:String(player.urlRequestStatus||''),
      button:rect&&rect.width>0&&rect.height>0?{x:rect.x+rect.width/2,y:rect.y+rect.height/2,obscured:!!hit&&!button.contains(hit)}:null});
  }
  return matches.length===1?matches[0]:{state:matches.length>1?'ambiguous':'unmatched'};
}
/* RF_AIGEI_PLAYER_END */

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (sender.id !== chrome.runtime.id) return false;
  if (sender.tab) {
    try {
      const page = new URL(sender.url);
      if (msg.action !== 'auto_pair' || sender.frameId !== 0 || page.protocol !== 'http:' ||
          page.hostname !== '127.0.0.1' || !page.port || page.search || page.hash ||
          !/^\/rf-connect\/[A-Za-z0-9_-]{43}$/.test(page.pathname)) return false;
    } catch { return false; }
  } else if (msg.action === 'auto_pair') return false;
  (async () => {
    try {
      if (msg.action === 'connect' || msg.action === 'auto_pair') {
        const value = validateAddress(msg.address);
        await chrome.storage.session.set({pair: value});
        if (address !== value || !socket || ![WebSocket.CONNECTING, WebSocket.OPEN].includes(socket.readyState)) connect(value);
        reply({accepted: true, message: statusMessage});
        return;
      } else if (msg.action === 'disconnect') {
        const old = socket; socket = null; address = '';
        for (const id of running.keys()) cancelJob(id);
        if (old) old.close();
        await chrome.storage.session.remove('pair');
        statusMessage = '已断开浏览器连接。';
      }
      reply({message: statusMessage});
    } catch (error) { reply({message: error.message}); }
  })();
  return true;
});
chrome.storage.session.get('pair').then(saved => { if (saved.pair && !socket) connect(saved.pair); });
