const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const {statusTarget,reportTransferUrl}=require('./status.js');
const nodes={};
const html=fs.readFileSync(__dirname+'/popup.html','utf8');
for(const id of [...html.matchAll(/id="([^"]+)"/g)].map(m=>m[1])){
 nodes[id]={value:'',dataset:{},disabled:id==='send',listeners:{},addEventListener(event,fn){this.listeners[event]=fn;}};
}
let calls=[],opened=[];
const context={document:{querySelector:selector=>{const n=nodes[selector.slice(1)];if(!n)throw Error('Missing popup control: '+selector);return n;}},URL,Date,statusTarget,reportTransferUrl,
 readPortalStatus:function(){},chrome:{tabs:{query:async()=>[{id:7,url:'https://zakup.gov.kz/cabinet/purchase/my-applications'}],create:async options=>opened.push(options)},
 runtime:{},scripting:{executeScript:async options=>{calls.push(options);return [{result:{schema:'goszakup-status-v1',state:'completed',number:'73727651'}}];}}}};
vm.createContext(context);
vm.runInContext(fs.readFileSync(__dirname+'/popup.js','utf8'),context);
(async()=>{
 // No JSON file loaded: read-only status checking must work independently of filling.
 nodes.company_bin.value='241040001415';
 nodes.announcement_number.value='17896678-1';
 nodes.lot_number.value='88391354-ЗЦП2';
 await nodes.status.onclick();
 assert.equal(calls.length,1);
 assert.deepEqual(JSON.parse(JSON.stringify(calls[0].args[0])),{
  company_bin:'241040001415',announcement_number:'17896678-1',lot_number:'88391354-ЗЦП2'});
 assert.equal(vm.runInContext('statusReport',context).state,'completed');
 assert.equal(nodes['result-title'].textContent,'Заявка завершена');
 assert.equal(nodes['result-card'].dataset.tone,'success');
 assert.equal(nodes.send.disabled,false);
 nodes['app-url'].value='http://localhost:8000';
 await nodes.send.onclick();
 assert.equal(opened.length,1);
 const transfer=new URL(opened[0].url);
 assert.equal(transfer.pathname,'/settings/extension/report/');
 assert.equal(transfer.search,'');
 assert.equal(JSON.parse(decodeURIComponent(transfer.hash.slice(1))).number,'73727651');
 nodes.lot_number.value='';nodes.lot_number.listeners.input();
 assert.equal(nodes.send.disabled,true);
 await nodes.status.onclick();
 assert.equal(calls.length,1); // Invalid targets must not execute in the portal.
 assert.match(nodes['result-summary'].textContent,/полный номер лота/);
 assert.equal(nodes['result-card'].dataset.tone,'error');
 context.job={packet:{company_bin:'241040001415',announcement_number:'17920228-1',lot_number:'88537093-ЗЦП1'},
  status:'blocked',number:'73769061',lotConfirmed:true,message:'Документы ещё загружаются'};
 vm.runInContext('renderDraft(job)',context);
 assert.equal(nodes['result-title'].textContent,'Черновик найден, заполнение остановлено');
 assert.match(nodes['result-summary'].textContent,/Документы ещё загружаются/);
 assert.equal(nodes['result-card'].dataset.tone,'warning');
 assert.equal(vm.runInContext('statusReport',context).description_saved,false);
 context.job.status='complete';context.job.message='Описание товара сохранено';vm.runInContext('renderDraft(job)',context);
 assert.equal(nodes['result-title'].textContent,'Описание товара заполнено');
 assert.equal(vm.runInContext('statusReport',context).description_saved,true);
 for(const id of ['file','fill','inspect','restriction','download'])assert.equal(nodes[id],undefined);
 assert.equal(nodes['draft-start'].disabled,false);
 assert.equal(nodes['draft-stop'].disabled,true);
 vm.runInContext('renderDraft(null)',context);
 for(const key of ['company_bin','announcement_number','lot_number'])assert.equal(nodes[key].value,'');
 assert.equal(vm.runInContext('statusReport',context),null);
 assert.equal(vm.runInContext('absenceTarget',context),undefined);
 assert.equal(nodes.send.disabled,true);
 assert.equal(nodes['draft-start'].disabled,true);
 assert.equal(nodes['result-card'].hidden,true);
 console.log('Simplified popup: actual HTML controls, status transfer, draft recovery and no obsolete tools passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
