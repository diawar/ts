const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const generation='11111111-1111-4111-8111-111111111111';
const button={disabled:false,addEventListener(type,fn){this.click=fn;}};
const nodes={'.queue-start':button,'.queue-repeat':{checked:false},'.queue-state':{},'.queue-detail':{},'.queue-remove':{}};
const row={dataset:{queueId:'1',generation,state:'queued',packetUrl:'/workspace/drafts/1/packet/',progressUrl:'/workspace/drafts/1/progress/'},querySelector:s=>nodes[s],querySelectorAll:s=>nodes[s]?[nodes[s]]:[]};
const output={},listeners=new Set(),requests=[];let snapshot=null,contextQueue=null,tick,sequence=0;
const window={addEventListener:(type,fn)=>listeners.add(fn),removeEventListener:(type,fn)=>listeners.delete(fn),postMessage(data){
 const reply=data.type==='ping'?{type:'ready',extensionId:'test',queueLaunchGuard:true}:data.type==='queue-status'?{type:'result',snapshot,queue_context:contextQueue}:{type:'result',prepared:true,opened:true};
 if(data.type==='prepare'){snapshot={status:'ready',company_bin:'241040001415',lot_number:'88537093-ЗЦП1',announcement_number:'17920228-1'};contextQueue=data.queue_context;}
 queueMicrotask(()=>{for(const fn of [...listeners])fn({source:window,origin:'http://localhost:8000',data:{channel:data.channel,id:data.id,...reply}});});
}};
const context={window,document:{querySelectorAll:()=>[row],querySelector:s=>s==='#queue-message'?output:s==='#queue-csrf [name=csrfmiddlewaretoken]'?{value:'csrf'}:null},location:{origin:'http://localhost:8000',href:'http://localhost:8000/workspace/drafts/'},crypto:{randomUUID:()=>('a'.repeat(25)+String(++sequence))},URL,JSON,FormData,setTimeout,clearTimeout,setInterval:fn=>tick=fn,
 fetch:async(url,options)=>{
  requests.push({url,options});
  return {ok:true,redirected:false,json:async()=>url.endsWith('/packet/')?{packet:{lot_number:'88537093-ЗЦП1'},queue_context:{id:1,generation}}:{saved:true,state:snapshot.status,label:snapshot.status==='complete'?'Черновик готов':'Передано в расширение'}};
 }};
vm.createContext(context);vm.runInContext(fs.readFileSync('src/tender_search/core/static/core/draft_queue.js','utf8'),context);
const settle=()=>new Promise(resolve=>setTimeout(resolve,10));
(async()=>{
 await settle();assert.equal(requests.length,0); // Viewing a queue does not start drafts.
 await button.click();await settle();assert.equal(requests.filter(r=>r.url.endsWith('/packet/')).length,1);
 assert.equal(row.dataset.state,'ready');
 assert.equal(nodes['.queue-remove'].disabled,false);
 snapshot={...snapshot,status:'complete',description_saved:true,lotConfirmed:true,number:'123',observed_at:new Date().toISOString(),message:'Готово'};
 tick();await settle();assert.equal(row.dataset.state,'complete');assert.equal(button.disabled,true);
 const count=requests.length;tick();await settle();assert.equal(requests.length,count); // Same browser event is not posted repeatedly.
 contextQueue={id:99,generation};tick();await settle();assert.equal(requests.length,count); // Another task is never applied to this row.
 assert.equal(listeners.size,0);
 console.log('Draft queue UI: explicit launch, progress persistence, completion, idempotent polling and task isolation passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
