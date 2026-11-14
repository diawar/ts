const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const generation='11111111-1111-4111-8111-111111111111';
const button=()=>({disabled:false,addEventListener(type,fn){this.click=fn;}});
const run=button(),stop=button(),out={},requests=[],transfers=[];
const rows=[1,2,3].map(id=>{const nodes={'.queue-start':button(),'.queue-repeat':{checked:false},'.queue-state':{},'.queue-detail':{},'.queue-remove':{}};return {nodes,dataset:{queueId:String(id),generation,state:'queued',inputReady:id===3?'false':'true',startReason:'Заполните данные',guardUrl:`/workspace/drafts/${id}/guard/`,packetUrl:`/workspace/drafts/${id}/packet/`,progressUrl:`/workspace/drafts/${id}/progress/`},querySelector:s=>nodes[s],querySelectorAll:s=>nodes[s]?[nodes[s]]:[]};});
let snapshot=null,queue_context=null,tick,sequence=0,prepareError='';const listeners=new Set();
const window={addEventListener:(t,fn)=>listeners.add(fn),removeEventListener:(t,fn)=>listeners.delete(fn),postMessage(data){
 let reply={type:'result'};
 if(data.type==='ping')reply={type:'ready',extensionId:'test',queueLaunchGuard:true,autoQueue:true};
 if(data.type==='queue-status')Object.assign(reply,{snapshot,queue_context});
 if(data.type==='prepare'&&prepareError)reply.error=prepareError;
 else if(data.type==='prepare'){assert.equal(data.auto_start,true);transfers.push(data.queue_context.id);queue_context=data.queue_context;snapshot={status:'running',lot_number:data.packet.lot_number};reply.prepared=true;}
 if(data.type==='queue-stop'){reply.stopped=true;reply.idle=!snapshot;}
 queueMicrotask(()=>{for(const fn of [...listeners])fn({source:window,origin:'http://localhost:8000',data:{...reply,channel:data.channel,id:data.id}});});
}};
const context={window,document:{querySelectorAll:()=>rows,querySelector:s=>({'#queue-run':run,'#queue-stop':stop,'#queue-message':out,'#queue-csrf [name=csrfmiddlewaretoken]':{value:'csrf'}})[s]},location:{origin:'http://localhost:8000',href:'http://localhost:8000/workspace/drafts/'},crypto:{randomUUID:()=>('a'.repeat(25)+String(++sequence))},URL,JSON,FormData,setTimeout,clearTimeout,setInterval:fn=>tick=fn,
 fetch:async(url,options)=>{requests.push(url);const id=Number(url.split('/')[5]);return {ok:true,redirected:false,json:async()=>url.endsWith('/guard/')?{released:true,state:'paused',generation}:url.endsWith('/packet/')?{packet:{lot_number:`lot-${id}`},queue_context:{id,generation}}:{saved:true,state:snapshot.status,label:snapshot.status}};}};
vm.createContext(context);vm.runInContext(fs.readFileSync('src/tender_search/core/static/core/draft_queue.js','utf8'),context);
const settle=()=>new Promise(r=>setTimeout(r,15));
(async()=>{
 await settle();assert.deepEqual(transfers,[]);assert.equal(rows[2].nodes['.queue-start'].disabled,true);
 await rows[2].nodes['.queue-start'].click();await settle();assert.deepEqual(transfers,[]);assert.match(out.textContent,/Заполните данные/);
 prepareError='Проверка: передача отклонена';run.click();await settle();assert.equal(out.textContent,prepareError);tick();await settle();assert.equal(out.textContent,prepareError);assert.deepEqual(transfers,[]);prepareError='';
 assert.equal(run.disabled,true);await stop.click();await settle();assert.equal(rows[0].dataset.state,'paused');
 run.click();await settle();assert.deepEqual(transfers,[1]);
 tick();await settle();assert.equal(rows[0].dataset.state,'running');assert.deepEqual(transfers,[1]);
 snapshot={status:'waiting',message:'Повтор через 1 мин'};tick();await settle();assert.equal(rows[0].dataset.state,'waiting');assert.deepEqual(transfers,[1]);assert.equal(run.disabled,true);assert.equal(stop.disabled,false);
 snapshot={status:'complete',message:'done'};tick();await settle();
 assert.equal(rows[0].dataset.state,'complete');assert.deepEqual(transfers,[1,2]);
 assert.ok(requests.indexOf('http://localhost:8000/workspace/drafts/1/progress/')<requests.indexOf('http://localhost:8000/workspace/drafts/2/packet/'));
 snapshot={status:'blocked',message:'country missing'};tick();await settle();assert.equal(stop.disabled,true);assert.equal(run.disabled,false);
 tick();await settle();assert.deepEqual(transfers,[1,2]);
 run.click();await settle();assert.deepEqual(transfers,[1,2,2]);
 await stop.click();snapshot={status:'complete',message:'done'};tick();await settle();assert.deepEqual(transfers,[1,2,2]);
 assert.equal(rows[1].dataset.state,'complete');run.click();await settle();assert.match(out.textContent,/Готовых к запуску лотов нет/);assert.equal(run.disabled,true);
 rows[2].dataset.state='cancelled';run.click();await settle();assert.match(out.textContent,/Очередь завершена/);
 console.log('Two-lot automatic queue: sequential launch, save-before-next, blocked stop, resume and explicit stop passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
