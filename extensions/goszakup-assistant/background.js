importScripts('draft.js','bridge-config.js','check.js');
let running=false;
let transferring=false;
const key='draftJob';
const readJob=async()=> (await chrome.storage.session.get(key))[key]||null;
let jobWrites=Promise.resolve();
function jobMutation(operation){
 const result=jobWrites.then(operation);jobWrites=result.catch(()=>{});return result;
}
async function clearJob(expected){
 return jobMutation(async()=>{
  const current=await readJob();
  if(expected&&current?.jobId!==expected.jobId)return;
  await chrome.storage.session.remove(key);await chrome.alarms.clear('draft-recovery');
  try{await chrome.action?.setBadgeText({text:''});}catch{}
 });
}
async function sourceIdentity(tabId,origin,queueContext=null,active=false){
 if(!Number.isInteger(tabId)||!globalThis.GOSZAKUP_APP_ORIGINS?.includes(origin))throw Error('Передайте подготовку заново из приложения.');
 let timer,identity;
 try{identity=await Promise.race([chrome.tabs.sendMessage(tabId,{type:'bridge:identity',queue_context:queueContext,queue_active:active}),
  new Promise((resolve,reject)=>{timer=setTimeout(()=>reject(Error('Вкладка приложения не отвечает.')),8000);})]);}
 finally{clearTimeout(timer);}
 if(identity?.origin!==origin||(!/^\/lots\/\d+\/preparation\/$/.test(identity?.pathname||'')&&identity?.pathname!=='/workspace/drafts/'))throw Error('Откройте исходную вкладку подготовки приложения.');
 if(identity?.error||!/^\d+$/.test(identity?.user_id||''))throw Error(identity?.error||'Не удалось подтвердить аккаунт приложения.');
 if(queueContext&&identity.queue_allowed!==true)throw Error('Запуск очереди больше не подтверждён.');
 return {appOrigin:origin,appTabId:tabId,appUserId:identity.user_id};
}
async function guardJob(job,active=false){
 if(!job)return null;
 try{
  if(!job.jobId||!job.appUserId)throw Error('Старая подготовка не привязана к аккаунту.');
  const current=await sourceIdentity(job.appTabId,job.appOrigin,job.queue_context,active);
  if(current.appUserId!==job.appUserId)throw Error('Аккаунт приложения изменился.');
  if((await readJob())?.jobId!==job.jobId)throw Error('Подготовка уже изменилась.');
  return job;
 }catch(error){
  await clearJob(job);
  throw Error('Подготовка скрыта: '+error.message+' Передайте её заново из текущего аккаунта.');
 }
}
async function senderIdentity(message,sender){
 const binding=await sourceIdentity(sender.tab.id,new URL(sender.url).origin);
 if(binding.appUserId!==message.account_id)throw Error('Аккаунт изменился. Откройте подготовку заново.');
 return binding;
}
const recoveryKey=packet=>'draftRecovery:'+packet.company_bin+':'+packet.announcement_number+':'+packet.lot_number;
async function recovery(packet,binding){
 const record=(await chrome.storage.local.get(recoveryKey(packet)))[recoveryKey(packet)];
 const own=record?.appOrigin===binding.appOrigin&&record?.appUserId===binding.appUserId;
 const number=(own?record?.number:'')||packet.existing_application_number||'';
 if(number&&!/^\d+$/.test(number))throw Error('Некорректный сохранённый номер черновика.');
 // Retain a company/lot duplicate guard without disclosing another user's number.
 return {number,createUncertain:record?.createUncertain===true||Boolean(!own&&record?.number&&record.number!==number)};
}
const writeJob=(job,replace=false)=>jobMutation(async()=>{
 if(replace)job={...job,jobId:crypto.randomUUID()};
 else if(!job?.jobId||(await readJob())?.jobId!==job.jobId)throw Error('Подготовка уже изменилась.');
 // Persist the minimal duplicate guard before an action, without product or bank data.
 if(job.number||job.createUncertain){
  const saved=(await chrome.storage.local.get(recoveryKey(job.packet)))[recoveryKey(job.packet)];
  const owner=job.number||!saved?.number?job:saved;
  await chrome.storage.local.set({[recoveryKey(job.packet)]:{number:job.number||saved?.number||'',createUncertain:job.createUncertain===true,appOrigin:owner.appOrigin,appUserId:owner.appUserId}});
 }
 else if(job.createUncertain===false)await chrome.storage.local.remove(recoveryKey(job.packet));
 await chrome.storage.session.set({[key]:job});return job;
});
const retryAlarm='draft-recovery';
const retryDelays=[1,2,4,8,15,15,15,15];
async function waitRecovery(job,reason){
 const current=await readJob();if(!current||current.stop||current.jobId!==job?.jobId)return;
 const attempt=job.retryCount||0;
 if(attempt>=retryDelays.length){await writeJob({...job,status:'blocked',message:'Предел автоматических повторов достигнут. '+reason});return;}
 const nextRetryAt=Date.now()+retryDelays[attempt]*60000;
 await writeJob({...job,status:'waiting',retryCount:attempt+1,nextRetryAt,
  message:'Ожидание восстановления. Проверка '+(attempt+1)+'/'+retryDelays.length+' через '+retryDelays[attempt]+' мин. '+reason});
 await chrome.alarms.create(retryAlarm,{when:nextRetryAt});
 if((await readJob())?.stop)await chrome.alarms.clear(retryAlarm);
}
async function resumeRecovery(){
 if(running||transferring)return;
 const job=await readJob();if(!job||job.stop||job.status!=='waiting')return;
 running=true;
 try{
  await guardJob(job);
  await writeJob({...job,status:'running',message:'Проверяем восстановление и существующий черновик…'});
  await new Promise((resolve,reject)=>{
   let timer;
   const done=error=>{clearTimeout(timer);chrome.tabs.onUpdated.removeListener(listener);error?reject(error):resolve();};
   const listener=(id,change)=>{if(id===job.tabId&&change.status==='complete')done();};
   chrome.tabs.onUpdated.addListener(listener);
   timer=setTimeout(()=>done(Error('Страница Госзакупа не загрузилась.')),30000);
   chrome.tabs.update(job.tabId,{url:'https://zakup.gov.kz/cabinet/announcement/'+job.packet.announcement_external_id}).catch(done);
  });
  await runJob();
 }catch(error){await waitRecovery(job,error.message);}
 finally{running=false;}
}
chrome.alarms.onAlarm.addListener(alarm=>{if(alarm.name===retryAlarm)void resumeRecovery();});
async function injected(job,action){
 await guardJob(job,true);
 const rows=await chrome.scripting.executeScript({target:{tabId:job.tabId},func:runDraftStep,
  args:[job.packet,action,{applicationNumber:job.number||'',lotConfirmed:job.lotConfirmed===true}]});
 if(rows.length!==1||!rows[0].result)throw Error('Ответ страницы не получен. Проверьте черновик на портале.');
 return rows[0].result;
}
async function navigatePortal(job,address){
 await guardJob(job);
 const url=new URL(address),path='/cabinet/purchase/announcements/'+job.packet.announcement_external_id+'/apply';
 const step=Number(url.searchParams.get('step')||'1'),id=url.searchParams.get('id');
 if(url.origin!=='https://zakup.gov.kz'||url.pathname!==path||url.username||url.password||url.hash||
   [...url.searchParams.keys()].some(key=>!['id','step'].includes(key))||url.searchParams.getAll('id').length>1||url.searchParams.getAll('step').length>1||
   !Number.isInteger(step)||step<1||step>3||(id!==null&&!/^\d+$/.test(id))||job.number&&id!==job.number)throw Error('Адрес перехода к черновику не подтверждён.');
 await new Promise((resolve,reject)=>{
  let timer;
  const done=error=>{clearTimeout(timer);chrome.tabs.onUpdated.removeListener(listener);error?reject(error):resolve();};
  const listener=(tabId,change)=>{if(tabId===job.tabId&&change.status==='complete')done();};
  chrome.tabs.onUpdated.addListener(listener);
  timer=setTimeout(()=>done(Error('Переход ещё не завершился. Проверьте открытую страницу и продолжите после загрузки.')),20000);
  chrome.tabs.update(job.tabId,{url:url.href}).catch(done);
 });
}
async function runJob(){
 let runId;
 try{
  runId=(await readJob())?.jobId;
  if(!runId)return;
  for(let step=0;step<24;step++){
   let job=await readJob();if(!job||job.jobId!==runId||job.stop) return;
   const probe=await injected(job,'probe');
   if((await readJob())?.stop)return;
   if(probe.blocked){if(probe.retryable)await waitRecovery(job,probe.message);else await writeJob({...job,status:'blocked',message:probe.message});return;}
   if(job.createUncertain&&!probe.number&&probe.next==='open_form'&&!probe.resuming){await waitRecovery(job,'Результат создания неизвестен. Проверяем существующий черновик; повторное создание запрещено.');return;}
   if((job.createUncertain||job.number)&&!probe.number&&probe.next==='save_account'){
    await writeJob({...job,status:'blocked',message:'Предыдущая попытка создания не подтверждена. Откройте объявление заново и продолжите существующую заявку; повторное создание отключено.'});return;
   }
   if(probe.number){job.number=probe.number;job.createUncertain=false;}
   if(probe.lotConfirmed)job.lotConfirmed=true;
   if(probe.state==='complete'){
    if(!job.number||!job.lotConfirmed)throw Error('Номер черновика и состав заявки не подтверждены.');
    await writeJob({...job,status:'complete',message:probe.message,observedAt:new Date().toISOString(),intent:''});return;
   }
   if(!probe.next)throw Error('Не определён следующий шаг. Продолжите вручную.');
   const current=await readJob();if(current?.jobId!==runId||current.stop)return;
   job={...job,status:'running',intent:probe.next,phase:({open_form:'portal',fill_account:'account',save_account:'account',add_lot:'lot',advance_lots:'lot',open_description:'description',edit_description:'description',fill_description:'description',save_description:'description'})[probe.next]||job.phase||'portal',message:probe.message,
    createUncertain:job.createUncertain||(probe.next==='save_account'&&!job.number)};
   await writeJob(job); // Durable intent before any portal action.
   const result=await injected(job,probe.next);
   // A confirmed preflight refusal means no create click happened. Transport
   // failures keep the durable uncertainty marker, even if the UI looks unchanged.
   if(probe.next==='save_account'&&!job.number&&result.blocked&&result.uncertain===false)job.createUncertain=false;
   if(result.number){job.number=result.number;job.createUncertain=false;}
   if(result.lotConfirmed)job.lotConfirmed=true;
   if(result.state==='draft_created')job.phase='lot';
   if(['documents_opened','description_opened','description_ready'].includes(result.state))job.phase='description';
   if(result.navigate){
    job={...job,status:'running',message:result.message,intent:'open_form'};await writeJob(job);
    await navigatePortal(job,result.navigate);
    const current=await readJob();if(current?.stop)return;
   }
   const stopped=(await readJob())?.stop;
   if(!stopped&&result.blocked&&result.retryable){await waitRecovery(job,result.message);return;}
   job={...job,stop:Boolean(stopped),status:stopped?'paused':result.blocked?'blocked':result.state==='complete'?'complete':'running',
    message:result.message,intent:result.blocked?job.intent:'',observedAt:new Date().toISOString()};
   await writeJob(job);
   if(result.blocked||result.state==='complete'||stopped)return;
  }
  const job=await readJob();if(job?.jobId===runId)await writeJob({...job,status:'paused',message:'Достигнут предел шагов. Проверьте форму и продолжите.'});
 }catch(error){
  const job=await readJob();if(job&&job.jobId===runId&&!job.stop){if(/Frame.*removed|document.*unloaded|message port closed|Cannot access contents|Переход ещё не завершился|Ответ страницы не получен/i.test(error.message))await waitRecovery(job,error.message);else await writeJob({...job,status:'blocked',message:error.message});}
 }finally{running=false;}
}
async function command(message){
 if(message.type==='draft:clear'){
  if(running||transferring)throw Error('Дождитесь остановки текущего шага и передачи подготовки.');
  await clearJob();return null;
 }
 const saved=await readJob();if(saved)await guardJob(saved);
 if(transferring&&['draft:load','draft:start','draft:clear'].includes(message.type))throw Error('Дождитесь завершения передачи из приложения.');
 if(message.type==='draft:get'){
  const job=await readJob();
  if(job?.status==='running'&&!running){await waitRecovery(job,'Обработка прервалась. Перед продолжением проверим существующий черновик.');return readJob();}
  if(job?.status==='waiting'&&job.nextRetryAt)await chrome.alarms.create(retryAlarm,{when:Math.max(Date.now()+1000,job.nextRetryAt)});
  return job;
 }
 if(message.type==='draft:load'){
  const binding=await sourceIdentity(message.appTabId,message.appOrigin);
  if(binding.appUserId!==message.appUserId)throw Error('Аккаунт приложения изменился.');
  if(running||(await readJob())?.status==='waiting')throw Error('Сначала остановите текущее заполнение.');
  validateDraftPacket(message.packet);
  const old=await readJob();
  if(old?.createUncertain)throw Error('Есть неподтверждённая попытка создания. Проверьте её на портале перед очисткой подготовки.');
  return writeJob({packet:message.packet,...binding,status:'ready',message:'Подготовка загружена. Откройте нужное объявление.',...await recovery(message.packet,binding),lotConfirmed:false},true);
 }
 if(message.type==='draft:stop'){
  await chrome.alarms.clear(retryAlarm);
  const job=await readJob();return job?writeJob({...job,stop:true,status:'paused',message:'Остановка запрошена. Текущий шаг может завершиться; следующие шаги отключены.'}):null;
 }
 if(message.type==='draft:confirm-absent'){
  if(running||transferring)throw Error('Сначала остановите заполнение и дождитесь завершения текущего шага.');
  const job=await readJob();
  if(!job||!['paused','blocked','ready'].includes(job.status)||!job.createUncertain||job.number||job.packet.existing_application_number)throw Error('Сначала остановите ожидание. Сброс доступен только для неподтверждённой попытки без номера заявки.');
  if(message.announcement_number!==job.packet.announcement_number||message.lot_number!==job.packet.lot_number||message.company_bin!==job.packet.company_bin)throw Error('Подготовка изменилась. Откройте расширение заново.');
  await chrome.alarms.clear(retryAlarm);
  return writeJob({...job,createUncertain:false,retryCount:0,nextRetryAt:null,intent:'',message:'Вы подтвердили отсутствие черновика в «Моих заявках». Вернитесь в очередь и нажмите «Запустить очередь».'});
 }
 if(message.type==='draft:start'){
  if(running||(await readJob())?.status==='waiting')throw Error('Заполнение или ожидание уже выполняется.');
  let job=await readJob();validateDraftPacket(job?.packet);
  const [tab]=await chrome.tabs.query({active:true,currentWindow:true});
  const url=new URL(tab?.url||'');
  if(url.origin!=='https://zakup.gov.kz'||!url.pathname.startsWith('/cabinet/'))throw Error('Откройте нужное объявление в кабинете zakup.gov.kz.');
  running=true;
  try{job=await writeJob({...job,tabId:tab.id,stop:false,status:'running',message:'Проверяем форму…'});}
  catch(error){running=false;throw error;}
  void runJob();return job;
 }
 throw Error('Неизвестная команда.');
}
function trustedPreparationSender(sender){
 try{
  const url=new URL(sender.url);
  return sender.id===chrome.runtime.id&&sender.frameId===0&&Number.isInteger(sender.tab?.id)&&
   globalThis.GOSZAKUP_APP_ORIGINS?.includes(url.origin)&&(/^\/lots\/\d+\/preparation\/$/.test(url.pathname)||url.pathname==='/workspace/drafts/');
 }catch{return false;}
}
async function receivePreparation(message,sender){
 const binding=await senderIdentity(message,sender);
 const existing=await readJob();
 if(existing){try{await guardJob(existing);}catch{}}
 if(running||transferring)throw Error('Сначала завершите или остановите текущее заполнение в расширении.');
 transferring=true;
 try{
  if((await readJob())?.status==='waiting')throw Error('Сначала остановите ожидание восстановления.');
  if(JSON.stringify(message.packet).length>50000)throw Error('Слишком большой пакет подготовки.');
  validateDraftPacket(message.packet);
  const old=await readJob();
  if(old?.createUncertain&&(old.packet.company_bin!==message.packet.company_bin||old.packet.lot_number!==message.packet.lot_number||old.packet.announcement_number!==message.packet.announcement_number))throw Error('Проверьте на портале предыдущую неподтверждённую попытку создания черновика.');
  const packet=message.packet;
  const queue=message.queue_context;
  if(message.auto_start===true&&!queue)throw Error('Автозапуск доступен только из очереди.');
  if(queue&&(!Number.isSafeInteger(queue.id)||queue.id<1||!/^[-a-f0-9]{36}$/.test(queue.generation)||new URL(sender.url).pathname!=='/workspace/drafts/'))throw Error('Некорректный пункт очереди.');
  if(packet.reset_absent_application!==undefined){
   if(!queue||!/^\d{1,30}$/.test(packet.reset_absent_application)||packet.existing_application_number)throw Error('Сброс старого номера не подтверждён очередью.');
   const record=(await chrome.storage.local.get(recoveryKey(packet)))[recoveryKey(packet)];
   if(record?.createUncertain)throw Error('Есть неподтверждённая попытка создания. Проверьте её отдельно.');
   if(record?.number===packet.reset_absent_application)await chrome.storage.local.remove(recoveryKey(packet));
  }
  const same=!packet.reset_absent_application&&old?.appOrigin===binding.appOrigin&&old?.appUserId===binding.appUserId&&old?.packet?.company_bin===packet.company_bin&&old?.packet?.announcement_number===packet.announcement_number&&old?.packet?.lot_number===packet.lot_number;
  await writeJob({packet,...binding,status:'ready',message:'Подготовка получена из приложения. Нажмите «Создать / продолжить черновик» на вкладке Госзакупа.',
   ...await recovery(packet,binding),number:(same?old.number:'')||(await recovery(packet,binding)).number,lotConfirmed:same&&old.lotConfirmed===true,queue_context:queue||null},true);
  let opened=false;
  try{
   const tab=await chrome.tabs.create({url:'https://zakup.gov.kz/cabinet/announcement/'+packet.announcement_external_id});opened=true;
   if(message.auto_start===true){
    running=true;
    await writeJob({...await readJob(),tabId:tab.id,stop:false,status:'running',message:'Открываем объявление…'});
    void (async()=>{
     try{
      await new Promise((resolve,reject)=>{
       const timer=setTimeout(()=>{chrome.tabs.onUpdated.removeListener(listener);reject(Error('Страница Госзакупа не загрузилась. Продолжите очередь после проверки сайта.'));},30000);
       const listener=(id,change)=>{if(id===tab.id&&change.status==='complete'){clearTimeout(timer);chrome.tabs.onUpdated.removeListener(listener);resolve();}};
       chrome.tabs.onUpdated.addListener(listener);
       chrome.tabs.get(tab.id).then(current=>{if(current.status==='complete')listener(tab.id,{status:'complete'});},reject);
      });
      await runJob();
     }catch(error){const job=await readJob();if(job)await waitRecovery(job,error.message);}
     finally{running=false;}
    })();
   }
  }catch(error){if(message.auto_start===true){running=false;throw error;}}
  try{await chrome.action.setBadgeText({text:'1'});await chrome.action.setBadgeBackgroundColor({color:'#087ee5'});}catch{}
  return {prepared:true,opened};
 }finally{transferring=false;}
}
chrome.runtime.onMessage.addListener((message,sender,respond)=>{
 if(message?.type==='bridge:queue-check'){
  if(!trustedPreparationSender(sender)||new URL(sender.url).pathname!=='/workspace/drafts/')return false;
  (async()=>{
   const binding=await senderIdentity(message,sender);
   const previous=await readJob();if(previous){try{await guardJob(previous);}catch{}}
   if(running||transferring||['running','waiting'].includes((await readJob())?.status))throw Error('Сначала остановите заполнение очереди.');
   transferring=true;
    let checkTab;
    try{
     const t=message.target;
    if(!t||!/^\d{12}$/.test(t.company_bin)||!/^\d{6,}-\d+$/.test(t.announcement_number)||!/^\d+-[А-Яа-яA-Za-z0-9]+$/.test(t.lot_number)||!/^\d{1,30}$/.test(t.number))throw Error('Некорректные данные проверки.');
    const tab=checkTab=await chrome.tabs.create({url:'https://zakup.gov.kz/cabinet/purchase/my-applications',active:false});
    await new Promise((resolve,reject)=>{
     const done=error=>{clearTimeout(timer);chrome.tabs.onUpdated.removeListener(listener);error?reject(error):resolve();};
     const listener=(id,change)=>{if(id===tab.id&&change.status==='complete')done();};
     const timer=setTimeout(()=>done(Error('Кабинет не загрузился.')),30000);chrome.tabs.onUpdated.addListener(listener);
     chrome.tabs.get(tab.id).then(current=>{if(current.status==='complete')done();},done);
    });
    const results=await chrome.scripting.executeScript({target:{tabId:tab.id},func:checkSavedApplication,args:[t]});
    const current=await senderIdentity(message,sender);
    if(current.appUserId!==binding.appUserId)throw Error('Аккаунт изменился. Откройте очередь заново.');
    if(results.length!==1||!results[0].result)throw Error('Ответ проверки не получен.');
     return {result:results[0].result};
    }finally{
     if(checkTab?.id!==undefined){try{await chrome.tabs.remove(checkTab.id);}catch{}}
     transferring=false;
    }
  })().then(respond,error=>respond({error:error.message}));return true;
 }
 if(message?.type==='bridge:queue-stop'){
  if(!trustedPreparationSender(sender)||new URL(sender.url).pathname!=='/workspace/drafts/')return false;
  readJob().then(async job=>{
   const binding=await senderIdentity(message,sender);
   if(job)await guardJob(job);
   if(job&&job.appUserId!==binding.appUserId)throw Error('Очередь другого аккаунта.');
   if(job&&job.appOrigin!==new URL(sender.url).origin)throw Error('Очередь другого приложения.');
   await command({type:'draft:stop'});
  }).then(()=>respond({stopped:true,idle:!running&&!transferring}),error=>respond({error:error.message}));return true;
 }
 if(message?.type==='bridge:queue-status'){
  if(!trustedPreparationSender(sender)||new URL(sender.url).pathname!=='/workspace/drafts/')return false;
  command({type:'draft:get'}).then(async job=>{
   const binding=await senderIdentity(message,sender);
   if(!job?.queue_context||job.appOrigin!==binding.appOrigin||job.appUserId!==binding.appUserId){respond({snapshot:null});return;}
  respond({queue_context:job.queue_context,snapshot:{status:job.stop&&(running||transferring)?'running':job.status,number:job.number||'',message:job.message||'',
    intent:job.intent||'',phase:job.phase||'',company_bin:job.packet.company_bin,announcement_number:job.packet.announcement_number,lot_number:job.packet.lot_number,
    lotConfirmed:job.lotConfirmed===true,description_saved:job.status==='complete',observed_at:job.observedAt||null}});
  }).catch(error=>respond({error:error.message}));return true;
 }
 if(message?.type==='bridge:prepare'){
  if(!trustedPreparationSender(sender))return false;
  receivePreparation(message,sender).then(respond,error=>respond({error:error.message}));return true;
 }
 if(sender.id!==chrome.runtime.id||sender.url!==chrome.runtime.getURL('popup.html'))return false;
 if(message.type==='draft:get')void chrome.action?.setBadgeText({text:''}).catch(()=>{});
 command(message).then(job=>respond({job}),error=>respond({error:error.message}));return true;
});
