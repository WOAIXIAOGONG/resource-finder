"""Self-contained connection assistant; no third-party requests or assets."""
PAGE = '''<!doctype html><html lang="zh-CN" data-rf-pair="__ADDRESS__"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>连接资源搜寻器</title><style>
:root{color-scheme:dark;font-family:"Segoe UI","Microsoft YaHei",sans-serif;background:#111318;color:#e8edf6}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:radial-gradient(ellipse at top,#202b48 0,transparent 65%)}
main{max-width:640px;width:calc(100% - 64px);padding:40px;margin:24px;box-sizing:border-box;border:1px solid #303744;border-radius:24px;background:#191c23;box-shadow:0 24px 80px #0005}
.brand{color:#8fa7ff;font-size:12px;letter-spacing:2px}h1{font-size:29px;letter-spacing:-.5px;margin:18px 0 10px}p{color:#a4b0c4;line-height:1.8;font-size:14px;margin:8px 0 20px}
.status{display:flex;align-items:center;gap:12px;border-radius:14px;padding:18px;background:#25304a;font-size:15px;line-height:1.6}
.dot{width:18px;height:18px;flex-shrink:0;border:2px solid #6c82b34d;border-top-color:#94acff;border-radius:50%;animation:spin 1s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}
.ready .dot{animation:none;border:0;background:#6bd4a3;width:12px;height:12px}.ready{background:#203d36}
#setup{border-top:1px solid #303744;margin-top:26px;padding-top:22px}h2{font-size:17px;margin:0 0 12px}ol{padding-left:22px;color:#c5cedf;font-size:14px;line-height:1.9}li{margin-bottom:14px}
button{background:#2b3445;border:1px solid #44516a;color:#ecf1ff;padding:10px 16px;border-radius:10px;cursor:pointer;font:inherit;margin:7px 7px 0 0;font-size:13px}button:hover{background:#3c4962}
code{display:block;margin:8px 0;padding:12px;background:#11141b;border:1px solid #323a4c;border-radius:9px;word-break:break-all;color:#9cb4ff;font-size:12px}
.note{font-size:12px;margin-bottom:0;color:#8997ae}.hidden{display:none!important}
</style></head><body><main>
<div class="brand">RESOURCE FINDER · 本机浏览器连接</div>
<h1>登录之后，其余交给工具</h1><p>保持已登录的 YouTube 页面打开。已有新版连接扩展时会自动连接，无需复制粘贴任何连接信息。</p>
<div class="status" id="status"><span class="dot"></span><span id="message">正在检测连接扩展…</span></div>
<section id="setup" class="hidden"><h2>首次使用：需要浏览器确认一次扩展安装</h2>
<p>浏览器不允许工具静默添加扩展。完成这一次安装后，后续点击工具里的“已登录”即可自动连接。</p>
<ol><li>打开你刚才登录网站的浏览器扩展管理页，开启“开发者模式”。<div>__BUTTONS__</div></li>
<li>点击“加载已解压的扩展 / 加载解压缩的扩展”，选择下面的工具自带文件夹。<code id="directory">__DIRECTORY__</code><button id="copy">复制扩展目录</button></li>
<li>回到此连接页面并刷新。工具会自动连接、核对登录状态并恢复搜索。<br>已安装旧版扩展时，在扩展管理页点击一次“重新加载”，再刷新此页面。</li></ol>
</section><p class="note">只检查已授权的 YouTube 页面，不导出密码或 Cookie；多个浏览器或独立资料需分别安装一次扩展。此连接仅在本机本轮验证期间有效。</p>
</main><script nonce="__NONCE__">
const base=location.pathname;let failures=0,connected=false;
const status=document.querySelector('#status'),message=document.querySelector('#message'),setup=document.querySelector('#setup');
const reveal=setTimeout(()=>{if(!connected){setup.classList.remove('hidden');message.textContent='尚未检测到连接扩展，请完成下方首次安装或更新。';}},2500);
const timer=setInterval(async()=>{try{const response=await fetch(base+'/status',{cache:'no-store'});if(!response.ok)throw Error();
 const data=await response.json();failures=0;if(data.connected){connected=true;clearTimeout(reveal);clearInterval(timer);setup.classList.add('hidden');status.classList.add('ready');message.textContent='浏览器已连接。工具正在检查实际搜索页面，请回到工具查看结果。';}}
 catch{if(++failures>=3){clearInterval(timer);clearTimeout(reveal);message.textContent='本次连接已结束或过期，请回到工具重新点击“已登录”。';setup.classList.add('hidden');status.querySelector('.dot').style.animation='none';}}},750);
document.querySelector('#copy').onclick=async()=>{try{await navigator.clipboard.writeText(document.querySelector('#directory').textContent);document.querySelector('#copy').textContent='目录已复制';}catch{message.textContent='请选中并复制上方扩展目录。';}};
for(const button of document.querySelectorAll('[data-browser]'))button.onclick=async()=>{try{const result=await fetch(base+'/extensions',{method:'POST',headers:{'Content-Type':'application/json','X-RF-Nonce':'__NONCE__'},body:JSON.stringify({browser:button.dataset.browser})});if(!result.ok)throw Error();}catch{message.textContent='请在浏览器中手动打开扩展管理页面。';}};
</script></body></html>'''
