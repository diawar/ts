const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('src/tender_search/core/static/core/draft_queue.js','utf8');
const settle=()=>new Promise(resolve=>setTimeout(resolve,15));
function setup({supported=true,extensionBusy=false,state='complete',checkReady=true,error='',checkedAt=''}={}){
 let now=Date.parse('2026-11-14T12:00:00Z'),tick,sequence=0;
 class Clock extends Date{constructor(...args){super(...(args.length?args:[now]));}static now(){return now;}}
 const nodes={'.queue-state':{},'.queue-detail':{},'.queue-check-result':{},'.queue-restart':{hidden:true}};
 const row={dataset:{queueId:'1',generation:'generation',state,checkReady:String(checkReady),checkedAt,checkState:'found',checkUrl:'/workspace/drafts/1/check/'},querySelector:s=>nodes[s],querySelectorAll:()=>[]};
 const listeners=new Set(),requests=[],messages=[],output={};
 const window={addEventListener:(t,f)=>listeners.add(f),removeEventListener:(t,f)=>listeners.delete(f),postMessage(data){
  messages.push(data.type);
  let result={type:'result'};
  if(data.type==='ping')result={type:'ready',extensionId:'test',backgroundQueueCheck:supported};
  if(data.type==='queue-status')result={...result,snapshot:extensionBusy?{status:'running'}:null,queue_context:null};
  if(data.type==='queue-check')result=error?{...result,error}:{...result,result:{...data.target,check_state:'found',observed_at:new Clock().toISOString(),message:'Найдена'}};
  queueMicrotask(()=>{for(const f of [...listeners])f({source:window,origin:'http://localhost:8000',data:{...result,channel:data.channel,id:data.id}});});
 }};
 const context={window,document:{querySelectorAll:()=>[row],querySelector:s=>s==='#queue-message'?output:s==='#queue-csrf [name=csrfmiddlewaretoken]'?{value:'csrf'}:null},
  location:{origin:'http://localhost:8000',href:'http://localhost:8000/workspace/drafts/'},Date:Clock,URL,JSON,FormData,Map,crypto:{randomUUID:()=>String(++sequence).padStart(20,'a')},setTimeout,clearTimeout,setInterval:fn=>tick=fn,
  fetch:async(url,options)=>{requests.push({url,options});const saving=options.headers['Content-Type']==='application/json';return {ok:true,redirected:false,json:async()=>saving?{saved:true,label:'Заявка найдена'}:{generation:'generation',target:{company_bin:'241040001415',announcement_number:'17920228-1',lot_number:'88537093-ЗЦП1',number:'123'}}};}};
 vm.runInNewContext(source,context);
 return {requests,messages,nodes,row,tick:()=>tick(),advance:ms=>now+=ms};
}
(async()=>{
 const app=setup();await settle();assert.equal(app.requests.length,2);assert.ok(app.messages.includes('queue-check'));assert.ok(!app.messages.includes('prepare'));
 app.advance(4000);app.tick();await settle();assert.equal(app.requests.length,2);
 app.advance(5*60*1000);app.tick();await settle();assert.equal(app.requests.length,4);
 assert.match(app.nodes['.queue-check-result'].textContent,/Заявка найдена/);
 for(const options of [{supported:false},{extensionBusy:true},{state:'running'},{state:'waiting'},{state:'cancelled'},{checkReady:false},{checkedAt:'2026-11-14T11:59:00Z'}]){
  const skipped=setup(options);await settle();assert.equal(skipped.requests.length,0,JSON.stringify(options));
 }
 const failed=setup({error:'Кабинет недоступен'});await settle();assert.equal(failed.requests.length,2);
 assert.equal(JSON.parse(failed.requests[1].options.body).result.check_state,'unknown');
 failed.advance(59000);failed.tick();await settle();assert.equal(failed.requests.length,2);
 failed.advance(1001);failed.tick();await settle();assert.equal(failed.requests.length,4);
 assert.ok(!failed.messages.includes('prepare'));
 console.log('Background draft checks: automatic read-only checks, five-minute cadence, throttled retries, legacy extension and active queue guards passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
