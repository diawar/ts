const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm');
const {validateDraftPacket,runDraftStep}=require('./draft.js');
const packet={schema:'goszakup-preparation-v1',company_bin:'241040001415',announcement_external_id:'17920228',
 announcement_number:'17920228-1',lot_number:'88537093-ЗЦП1',purchase_method_id:12,single_position:true,
 data:{product_name:'ТЕСТ комплект',manufacturer:'ТЕСТ изготовитель',country:'Китай',legal_address:'Тестовая 1',bank_account:'KZ000000000000000000',contact_phone:'+77000000000'}};
validateDraftPacket(packet);
validateDraftPacket({...packet,position_count:2});
validateDraftPacket({...packet,position_count:2,repeat_product:true});
for(const bad of [{purchase_method_id:11},{single_position:false},{announcement_external_id:'99'},
 {company_bin:'invalid'},{data:{...packet.data,manufacturer:''}},{data:{...packet.data,bank_account:'bad'}}])assert.throws(()=>validateDraftPacket({...packet,...bad}));
function worker(step,local={}){
 let listener,data={},calls=[],opened=[],updates=[],navigationListener,alarmListener,alarm=null;
 let account='1',pageAccount='1';
 const chrome={alarms:{create:async(name,value)=>{alarm={name,...value};},clear:async()=>{alarm=null;},onAlarm:{addListener:fn=>alarmListener=fn}},runtime:{id:'test-extension',getURL:p=>'chrome-extension://test-extension/'+p,onMessage:{addListener:fn=>listener=fn}},
 storage:{local:{get:async()=>structuredClone(local),set:async x=>Object.assign(local,structuredClone(x)),remove:async k=>{delete local[k];}},session:{get:async()=>structuredClone(data),set:async x=>{data=structuredClone({...data,...x});},remove:async k=>{delete data[k];}}},
 tabs:{query:async()=>[{id:7,url:'https://zakup.gov.kz/cabinet/announcement/17920228'}],create:async options=>{opened.push(options);return {id:99};},get:async id=>({status:'complete',url:id===11?'http://localhost:8000/lots/27723/preparation/':'https://zakup.gov.kz/cabinet/announcement/17920228'}),
 sendMessage:async()=>account===pageAccount?{user_id:account,origin:'http://localhost:8000',pathname:'/lots/27723/preparation/'}:{error:'Аккаунт изменился'},
 onUpdated:{addListener:fn=>navigationListener=fn,removeListener:fn=>{if(navigationListener===fn)navigationListener=null;}},
 update:async(id,options)=>{updates.push({id,...options});setTimeout(()=>navigationListener?.(id,{status:'complete'}),0);}},
 scripting:{executeScript:async({args})=>{calls.push(args[1]);return [{result:await step(args[1])}];}}};
 const context={chrome,validateDraftPacket,runDraftStep,URL,Date,setTimeout,clearTimeout,crypto:require('node:crypto').webcrypto,GOSZAKUP_APP_ORIGINS:['http://localhost:8000'],importScripts(){}};
 vm.createContext(context);vm.runInContext(fs.readFileSync(__dirname+'/background.js','utf8'),context);
 const sender={id:chrome.runtime.id,url:chrome.runtime.getURL('popup.html')};
 return {calls,opened,updates,switchAccount(value,stale=false){account=String(value);if(!stale)pageAccount=account;},seed(job){data.draftJob=structuredClone(job);},get alarm(){return alarm;},fire:async()=>{const a=alarm;alarm=null;alarmListener(a);await settle();},get job(){return data.draftJob;},send:(message,from=sender)=>new Promise(resolve=>{
  if(message.type==='draft:load')message={appTabId:11,appOrigin:'http://localhost:8000',appUserId:account,...message};
  if(message.type.startsWith('bridge:'))message={account_id:pageAccount,...message};
  if(listener(message,from,resolve)===false)resolve({ignored:true});}),
  untrusted:()=>listener({type:'draft:start'},{id:'other',url:'https://zakup.gov.kz/'},()=>{throw Error('Unauthorized response');})};
}
const settle=()=>new Promise(resolve=>setTimeout(resolve,15));
(async()=>{
 let stage=0;
 const happy=worker(async action=>{
  if(action==='probe')return stage===0?{next:'save_account'}:{state:'complete',number:'123',lotConfirmed:true,message:'ready'};
  stage++;return {state:'draft_created',number:'123',lotConfirmed:true};
 });
 assert.equal(happy.untrusted(),false);
 await happy.send({type:'draft:load',packet});await happy.send({type:'draft:start'});await settle();
 assert.equal(happy.job.status,'complete');assert.equal(happy.job.number,'123');assert.equal(happy.job.lotConfirmed,true);
 let navigated=false;
 const resume=worker(async action=>action==='probe'?(navigated?{state:'complete',number:'123',lotConfirmed:true}:{next:'open_form'}):
  (navigated=true,{state:'navigate',number:'123',navigate:'https://zakup.gov.kz/cabinet/purchase/announcements/17920228/apply?step=3&id=123'}));
 await resume.send({type:'draft:load',packet});await resume.send({type:'draft:start'});await settle();
 assert.equal(resume.job.status,'complete');assert.equal(resume.updates.length,1);assert.equal(resume.updates[0].id,7);assert.equal(resume.opened.length,0);
 const unsafe=worker(async action=>action==='probe'?{next:'open_form'}:{navigate:'https://evil.example/',number:'123'});
 await unsafe.send({type:'draft:load',packet});await unsafe.send({type:'draft:start'});await settle();
 assert.equal(unsafe.job.status,'blocked');assert.equal(unsafe.updates.length,0);
 let mutationCalls=0;
 const uncertain=worker(async action=>{if(action==='probe')return {next:'save_account'};mutationCalls++;throw Error('Navigation lost');});
 await uncertain.send({type:'draft:load',packet});await uncertain.send({type:'draft:start'});await settle();
 assert.equal(uncertain.job.createUncertain,true);assert.equal(mutationCalls,1);
 await uncertain.send({type:'draft:start'});await settle();assert.equal(mutationCalls,1);assert.equal(uncertain.job.status,'blocked');
 assert.equal(uncertain.job.status,'blocked');
 assert.ok((await uncertain.send({type:'draft:confirm-absent',...packet,announcement_number:'999-1'})).error);
 const reset=await uncertain.send({type:'draft:confirm-absent',company_bin:packet.company_bin,announcement_number:packet.announcement_number,lot_number:packet.lot_number});
 assert.equal(reset.job.createUncertain,false);assert.equal(reset.job.status,'blocked');
 // Restore an ambiguous creation for the remaining duplicate-guard checks.
 await uncertain.send({type:'draft:start'});await settle();
 const denied=await uncertain.send({type:'draft:load',packet});assert.match(denied.error,/неподтвержд/);
 const preflight=worker(async action=>action==='probe'?{next:'save_account'}:{blocked:true,uncertain:false,message:'Required choice missing'});
 await preflight.send({type:'draft:load',packet});await preflight.send({type:'draft:start'});await settle();
 assert.equal(preflight.job.createUncertain,false); // No click: a corrected form can be retried.
 const bad=await happy.send({type:'draft:load',packet:{...packet,purchase_method_id:99}});assert.ok(bad.error);
 const appSender={id:'test-extension',url:'http://localhost:8000/lots/27723/preparation/',frameId:0,tab:{id:11}};
 const bridge=worker(()=>{throw Error('Handoff must never inject into the portal');});
 const prepared=await bridge.send({type:'bridge:prepare',packet},appSender);
 assert.equal(prepared.prepared,true);assert.equal(prepared.opened,true);
 assert.equal(bridge.calls.length,0);assert.equal(bridge.job.status,'ready');
 assert.equal(bridge.job.appOrigin,'http://localhost:8000');
 assert.equal(bridge.opened[0].url,'https://zakup.gov.kz/cabinet/announcement/17920228');
 for(const from of [{...appSender,url:'https://evil.example/lots/27723/preparation/'},
  {...appSender,url:'http://localhost:8001/lots/27723/preparation/'},
  {...appSender,url:'http://localhost:8000/settings/'},{...appSender,frameId:1},{...appSender,id:'other'}]){
  assert.equal((await bridge.send({type:'bridge:prepare',packet},from)).ignored,true);
 }
 assert.equal(bridge.opened.length,1);
 const refused=await uncertain.send({type:'bridge:prepare',packet:{...packet,lot_number:'999-ЗЦП1'}},appSender);assert.match(refused.error,/неподтвержд/);assert.equal(uncertain.opened.length,0);
 const resumed=await happy.send({type:'bridge:prepare',packet},appSender);assert.equal(resumed.prepared,true);assert.equal(happy.job.number,'123');
 const queueSender={...appSender,url:'http://localhost:8000/workspace/drafts/'};
 const contextQueue={id:12,generation:'11111111-1111-4111-8111-111111111111'};
 const queued=await happy.send({type:'bridge:prepare',packet,queue_context:contextQueue},queueSender);
 assert.equal(queued.prepared,true);
 const resetWorker=worker(async()=>({next:'open_form'}));
 await resetWorker.send({type:'draft:load',packet:{...packet,existing_application_number:'123'}});
 const resetPacket={...packet,existing_application_number:'',reset_absent_application:'123'};
 await resetWorker.send({type:'bridge:prepare',packet:resetPacket,queue_context:contextQueue},queueSender);
 assert.equal(resetWorker.job.number,'');
 // Repeating a reset receipt must preserve a newer confirmed draft.
 await resetWorker.send({type:'draft:clear'});
 await resetWorker.send({type:'draft:load',packet:{...packet,existing_application_number:'999'}});
 await resetWorker.send({type:'bridge:prepare',packet:resetPacket,queue_context:contextQueue},queueSender);
 assert.equal(resetWorker.job.number,'999');
 const summary=await happy.send({type:'bridge:queue-status'},queueSender);
 assert.equal(summary.queue_context.id,12);assert.equal(summary.snapshot.number,'123');
 assert.equal(summary.snapshot.status,'ready');assert.equal(summary.snapshot.description_saved,false);
 assert.equal(summary.snapshot.data,undefined);assert.equal(summary.snapshot.packet,undefined);
 assert.equal((await happy.send({type:'bridge:queue-status'},appSender)).ignored,true);
 const invalid=await happy.send({type:'bridge:prepare',packet,queue_context:{id:-1,generation:'bad'}},queueSender);
 assert.ok(invalid.error);
 const auto=worker(async()=>({state:'complete',number:'456',lotConfirmed:true,message:'done'}));
 await auto.send({type:'bridge:prepare',packet,queue_context:contextQueue,auto_start:true},queueSender);await settle();
 assert.equal(auto.job.status,'complete');assert.equal(auto.job.tabId,99);assert.deepEqual(auto.calls,['probe']);
 const autoBlocked=worker(async()=>({blocked:true,message:'Login required'}));
 await autoBlocked.send({type:'bridge:prepare',packet,queue_context:contextQueue,auto_start:true},queueSender);await settle();
 assert.equal(autoBlocked.job.status,'blocked');assert.equal(autoBlocked.calls.length,1);
 assert.ok((await autoBlocked.send({type:'bridge:prepare',packet,auto_start:true},appSender)).error);
 const disk={};
 const before=worker(async action=>{if(action==='probe')return {next:'save_account'};throw Error('Browser crashed');},disk);
 await before.send({type:'draft:load',packet});await before.send({type:'draft:start'});await settle();
 const after=worker(async()=>({next:'save_account'}),disk);
 await after.send({type:'draft:load',packet});await after.send({type:'draft:start'});await settle();
 assert.deepEqual(after.calls,['probe']);assert.equal(after.job.status,'blocked');
 const known=worker(async()=>({next:'save_account'}));
 await known.send({type:'draft:load',packet:{...packet,existing_application_number:'73771733'}});
 await known.send({type:'draft:start'});await settle();
 assert.deepEqual(known.calls,['probe']);assert.equal(known.job.number,'73771733');
 assert.equal(JSON.stringify(disk).includes('bank_account'),false);
 let down=true,created=0;
 const retry=worker(async action=>{
  if(down)return {blocked:true,retryable:true,uncertain:false,message:'Сервис КГД недоступен'};
  if(action==='probe')return created?{state:'complete',number:'777',lotConfirmed:true}:{next:'save_account'};
  created++;return {number:'777',lotConfirmed:true};
 });
 await retry.send({type:'draft:load',packet});await retry.send({type:'draft:start'});await settle();
 assert.equal(retry.job.status,'waiting');assert.equal(created,0);assert.ok(retry.alarm.when>Date.now());
 await retry.fire();assert.equal(retry.job.retryCount,2);assert.equal(created,0);
 down=false;await retry.fire();assert.equal(retry.job.status,'complete');assert.equal(created,1);
 const waiting=worker(async()=>({blocked:true,retryable:true,message:'Unavailable'}));
 await waiting.send({type:'draft:load',packet});await waiting.send({type:'draft:start'});await settle();
 for(let i=0;i<8;i++)await waiting.fire();
 assert.equal(waiting.job.status,'blocked');assert.equal(waiting.alarm,null);
 const stop=worker(async()=>({blocked:true,retryable:true,message:'Unavailable'}));
 await stop.send({type:'draft:load',packet});await stop.send({type:'draft:start'});await settle();
 await stop.send({type:'draft:stop'});assert.equal(stop.job.status,'paused');assert.equal(stop.alarm,null);
 let unknownPhase=0,createRequests=0;
 const ambiguous=worker(async action=>{
  if(action==='probe')return unknownPhase===0?{next:'save_account'}:unknownPhase===1?{next:'open_form',resuming:false}:{state:'complete',number:'888',lotConfirmed:true};
  createRequests++;unknownPhase=1;return {blocked:true,retryable:true,uncertain:true,message:'Сервис недоступен'};
 });
 await ambiguous.send({type:'draft:load',packet});await ambiguous.send({type:'draft:start'});await settle();
 await ambiguous.fire();assert.equal(createRequests,1);assert.equal(ambiguous.job.status,'waiting');assert.equal(ambiguous.job.createUncertain,true);
 unknownPhase=2;await ambiguous.fire();assert.equal(ambiguous.job.status,'complete');assert.equal(createRequests,1);
 // Switching accounts in one Chrome profile must remove the old packet before
 // the popup or a different account's queue can inspect it.
 const privateDisk={};
 const switched=worker(async()=>({next:'save_account'}),privateDisk);
 await switched.send({type:'draft:load',packet:{...packet,existing_application_number:'123'}});
 switched.switchAccount('2');
 const hidden=await switched.send({type:'draft:get'});
 assert.ok(hidden.error);assert.equal(hidden.job,undefined);assert.equal(switched.job,undefined);
 assert.equal(JSON.stringify(hidden).includes(packet.data.contact_phone),false);
 assert.equal(JSON.stringify(privateDisk).includes('product_name'),false);
 assert.equal(Object.values(privateDisk)[0].number,'123'); // Keep the minimal duplicate guard.
 await switched.send({type:'draft:load',packet});
 assert.equal(switched.job.number,'');assert.equal(switched.job.createUncertain,true);
 await switched.send({type:'draft:clear'});await switched.send({type:'draft:load',packet});
 assert.equal(switched.job.number,''); // Clearing/reloading must not adopt A's recovery number.
 const legacy=worker(async()=>({next:'save_account'}));
 legacy.seed({packet,status:'ready'});
 assert.ok((await legacy.send({type:'draft:get'})).error);assert.equal(legacy.job,undefined);
 const queueSwitch=worker(async()=>({next:'save_account'}));
 await queueSwitch.send({type:'bridge:prepare',packet,queue_context:contextQueue},queueSender);
 queueSwitch.switchAccount('2');
 const oldSnapshot=await queueSwitch.send({type:'bridge:queue-status'},queueSender);
 assert.equal(oldSnapshot.snapshot,undefined);assert.equal(queueSwitch.job,undefined);
 const stalePage=worker(async()=>({next:'save_account'}));
 stalePage.switchAccount('2',true);
 assert.ok((await stalePage.send({type:'bridge:prepare',packet},appSender)).error);
 assert.equal(stalePage.job,undefined);assert.equal(stalePage.opened.length,0);
 const wrongAccount=worker(async()=>({next:'save_account'}));
 wrongAccount.switchAccount('2');
 assert.ok((await wrongAccount.send({type:'bridge:prepare',packet,account_id:'1'},appSender)).error);
 const waitSwitch=worker(async()=>({blocked:true,retryable:true,message:'Unavailable'}));
 await waitSwitch.send({type:'draft:load',packet});await waitSwitch.send({type:'draft:start'});await settle();
 waitSwitch.switchAccount('2');await waitSwitch.fire();
 assert.equal(waitSwitch.job,undefined);assert.equal(waitSwitch.updates.length,0);
 let finishMutation;
 const delayed=worker(async action=>action==='probe'?{next:'save_account'}:new Promise(resolve=>{finishMutation=resolve;}));
 await delayed.send({type:'draft:load',packet});await delayed.send({type:'draft:start'});await settle();
 assert.equal(typeof finishMutation,'function');
 delayed.switchAccount('2');await delayed.send({type:'draft:get'});
 finishMutation({number:'999',lotConfirmed:true});await settle();
 assert.equal(delayed.job,undefined); // A late portal response cannot resurrect account A's data.
 let resolveOld;
 const replacement=worker(async action=>action==='probe'?{next:'save_account'}:new Promise(resolve=>{resolveOld=resolve;}));
 await replacement.send({type:'draft:load',packet});await replacement.send({type:'draft:start'});await settle();
 replacement.switchAccount('2');
 replacement.seed({...replacement.job,jobId:'new-account-job',appUserId:'2',status:'ready',number:'',packet:{...packet,data:{...packet.data,product_name:'Owner B'}}});
 resolveOld({number:'111',lotConfirmed:true});await settle();
 assert.equal(replacement.job.status,'ready');assert.equal(replacement.job.number,'');assert.equal(replacement.job.packet.data.product_name,'Owner B');
 console.log('Draft packet validation, sender restriction, confirmed completion and duplicate creation guard passed');
 console.log('Account switching: popup isolation, queue isolation, stale source tabs, recovery alarms and late-response fencing passed');
})().catch(error=>{console.error(error);process.exitCode=1;});

module.exports={packet};
