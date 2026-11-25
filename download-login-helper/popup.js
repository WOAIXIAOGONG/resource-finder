const allowed=new Set(['youtube.com','bilibili.com','pixiv.net']);
document.getElementById('export').addEventListener('click',async()=>{
 const status=document.getElementById('status');const button=document.getElementById('export');button.disabled=true;
 try{
  const domain=document.getElementById('platform').value;if(!allowed.has(domain))throw new Error('不支持的平台');
  const cookies=(await chrome.cookies.getAll({domain})).filter(c=>c.domain.replace(/^\./,'')===domain||c.domain.endsWith('.'+domain));
  if(!cookies.length)throw new Error('未找到该平台Cookie，请先在当前浏览器登录。');
  const lines=['# Netscape HTTP Cookie File','# Local export for Resource Finder. Keep private.'];
  for(const c of cookies){
   if(/[\t\r\n]/.test(c.name+c.value+c.domain+c.path))continue;
   const host=(c.httpOnly?'#HttpOnly_':'')+c.domain;
   lines.push([host,c.domain.startsWith('.')?'TRUE':'FALSE',c.path,c.secure?'TRUE':'FALSE',c.session?'':Math.floor(c.expirationDate||0),c.name,c.value].join('\t'));
  }
  const url=URL.createObjectURL(new Blob([lines.join('\n')+'\n'],{type:'text/plain;charset=utf-8'}));
  try{await chrome.downloads.download({url,filename:'resource-finder-'+domain+'-cookies.txt',saveAs:true});}
  finally{setTimeout(()=>URL.revokeObjectURL(url),60000);}
  status.textContent='已发起本地保存，请回工具选择保存的文件。是否能下载仍取决于平台对当前会话的验证。';
 }catch(error){status.textContent=error.message||String(error);}finally{button.disabled=false;}
});
