const elements={platform:{value:'youtube.com'},status:{textContent:''},export:{disabled:false,addEventListener(_event,fn){this.click=fn;}}};
globalThis.document={getElementById:id=>elements[id]};
let body='',downloaded=0,requested='';
globalThis.URL.createObjectURL=blob=>{body=blob;return 'blob:test';};
globalThis.URL.revokeObjectURL=()=>{};
globalThis.setTimeout=()=>0;
globalThis.chrome={cookies:{getAll:async({domain})=>{requested=domain;return [
 {domain:'.youtube.com',name:'session',value:'private',path:'/',secure:true,httpOnly:true,session:true},
 {domain:'.unrelated.com',name:'no',value:'never',path:'/',secure:true,expirationDate:9999999999}
];}},downloads:{download:async args=>{if(!args.saveAs)throw new Error('must choose local destination');downloaded++;}}};
new Function(await Deno.readTextFile('download-login-helper/popup.js'))();
await elements.export.click();
const text=await body.text();
if(requested!=='youtube.com'||downloaded!==1||text.includes('unrelated')||!text.includes('#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t\tsession\tprivate'))throw new Error('cookie scope/session export failed');
elements.platform.value='evil.test';await elements.export.click();
if(downloaded!==1)throw new Error('unlisted platform exported');
console.log('PASS: explicit click, exact domain scope, HttpOnly/session formatting, local save only');
