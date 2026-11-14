const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),{randomUUID}=require('node:crypto');
const pageSource=fs.readFileSync('src/tender_search/core/static/core/preparation_bridge.js','utf8');
const bridgeSource=fs.readFileSync('extensions/goszakup-assistant/bridge.js','utf8');
const packet={schema:'goszakup-preparation-v1',company_bin:'241040001415',announcement_external_id:'17920228',
 announcement_number:'17920228-1',lot_number:'88537093-ЗЦП1',position_count:2,data:{product_name:'ТЕСТ'}};
function setup({installed=true,origin='http://localhost:8000',path='/lots/27723/preparation/',page=true}={}){
 const handlers=new Set(),sent=[],fetches=[];
 const nodes={};for(const id of ['prepare-with-extension','preparation-bridge-result','preparation-form','preparation-repeat-product'])
  nodes['#'+id]={disabled:false,checked:true,dataset:{exportUrl:'/lots/27723/preparation/export/'},listeners:{},addEventListener(type,fn){this.listeners[type]=fn;}};
 const window={addEventListener(type,fn){if(type==='message')handlers.add(fn);},removeEventListener(type,fn){handlers.delete(fn);},
  postMessage(data,target){assert.equal(target,origin);setImmediate(()=>{for(const fn of [...handlers])void fn({data,origin,source:window});});}};
 window.top=window;
 const context={window,document:{readyState:'complete',body:{dataset:{userId:'1'}},querySelector:key=>nodes[key]},location:{origin,pathname:path,href:origin+path},
  GOSZAKUP_APP_ORIGINS:['http://localhost:8000'],URL,JSON,AbortSignal,crypto:{randomUUID},setTimeout:installed?setTimeout:(fn)=>setTimeout(fn,0),clearTimeout,
  chrome:{runtime:{id:'abcdefghijklmnopabcdefghijklmnop',onMessage:{addListener(){}},sendMessage:async message=>{sent.push(message);return {prepared:true,opened:true};}}},
  fetch:async(url,options)=>{if(url==='/account/identity/')return {ok:true,json:async()=>({user_id:'1'})};fetches.push({url,options});return {ok:true,redirected:false,headers:{get:()=> 'application/json'},json:async()=>structuredClone(packet)};}};
 vm.createContext(context);
 if(installed){vm.runInContext(bridgeSource,context);vm.runInContext(bridgeSource,context);}
 if(page)vm.runInContext(pageSource,context);
 return {nodes,sent,fetches,window,handlers};
}
(async()=>{
 const app=setup(),button=app.nodes['#prepare-with-extension'];
 const first=button.listeners.click();await button.listeners.click();await first;
 assert.equal(app.sent.length,1);assert.equal(app.fetches.length,1);
 assert.equal(app.sent[0].type,'bridge:prepare');assert.equal(app.sent[0].packet.repeat_product,undefined);
 assert.equal(app.sent[0].account_id,'1');
 assert.equal(app.fetches[0].url,'http://localhost:8000/lots/27723/preparation/export/');
 assert.equal(app.fetches[0].options.credentials,'same-origin');
 assert.match(app.nodes['#preparation-bridge-result'].textContent,/Данные переданы/);
 assert.equal(button.disabled,false);
 const dirty=setup();dirty.nodes['#preparation-form'].listeners.input();await dirty.nodes['#prepare-with-extension'].listeners.click();
 assert.equal(dirty.fetches.length,0);assert.equal(dirty.sent.length,0);assert.match(dirty.nodes['#preparation-bridge-result'].textContent,/Сначала сохраните/);
 const missing=setup({installed:false});await missing.nodes['#prepare-with-extension'].listeners.click();assert.equal(missing.fetches.length,0);assert.match(missing.nodes['#preparation-bridge-result'].textContent,/не найдено/);
 assert.equal(setup({origin:'http://localhost:8001',page:false}).handlers.size,0);
 assert.equal(setup({path:'/settings/',page:false}).handlers.size,0);
 const readOnly=setup({page:false});
 const request={channel:'goszakup-preparation-bridge-v1',id:randomUUID(),type:'prepare',packet,extensionId:'abcdefghijklmnopabcdefghijklmnop'};
 for(const fn of readOnly.handlers){await fn({data:request,origin:'https://evil.example',source:readOnly.window});await fn({data:request,origin:'http://localhost:8000',source:{}});}
 assert.equal(readOnly.sent.length,0);
 console.log('Direct preparation bridge: authenticated fetch, no file, duplicate-click guard, dirty form and origin restrictions passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
