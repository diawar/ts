(()=>{
 const rows=[...document.querySelectorAll('[data-queue-id]')],output=document.querySelector('#queue-message');
 const csrf=document.querySelector('#queue-csrf [name=csrfmiddlewaretoken]').value;
 const channel='goszakup-preparation-bridge-v1';
 let extensionId,autoSupported=false,checkSupported=false,resetSupported=false,busy=false,polling=false,last='',automatic=false,activeRow=null,stopping=false,launchError='';
 const checkButton=document.querySelector('#queue-check');
 const runButton=document.querySelector('#queue-run'),stopButton=document.querySelector('#queue-stop');
 const canLaunch=row=>row.dataset.inputReady!=='false'&&!['complete','cancelled','running','waiting'].includes(row.dataset.state);
 const controls=()=>{
  if(checkButton)checkButton.disabled=automatic||busy||stopping;
  if(runButton)runButton.disabled=automatic||busy||stopping||!rows.some(canLaunch);
  if(stopButton)stopButton.disabled=!automatic||stopping;
  for(const row of rows){
   const working=['running','waiting'].includes(row.dataset.state);
   const start=row.querySelector('.queue-start');if(start)start.disabled=!canLaunch(row)||automatic||busy||stopping;
   const remove=row.querySelector('.queue-remove');if(remove)remove.disabled=working;
   const edit=row.querySelector('.queue-prepare');if(edit){if(working){edit.setAttribute('aria-disabled','true');edit.setAttribute('tabindex','-1');}else{edit.removeAttribute('aria-disabled');edit.removeAttribute('tabindex');}}
  }
 };
 const bridge=(type,extra={})=>new Promise((resolve,reject)=>{
  const id=crypto.randomUUID();let timer;
  const done=()=>{clearTimeout(timer);window.removeEventListener('message',listener);};
  const listener=event=>{
   const data=event.data;
   if(event.source!==window||event.origin!==location.origin||data?.channel!==channel||data.id!==id||data.type!==(type==='ping'?'ready':'result'))return;
   done();data.error?reject(Error(data.error)):resolve(data);
  };
  window.addEventListener('message',listener);
  timer=setTimeout(()=>{done();extensionId=undefined;reject(Error('Нет ответа расширения на этой вкладке. Проверьте, что оно включено в этом профиле Chrome и имеет доступ к сайту приложения.'));},type==='ping'?1800:type==='queue-check'?65000:12000);
  window.postMessage({channel,id,type,extensionId,...extra},location.origin);
 });
 const connect=async()=>{if(!extensionId){const result=await bridge('ping');extensionId=result.extensionId;autoSupported=result.autoQueue===true;checkSupported=result.queueCheck===true;resetSupported=result.queueReset===true;if(!extensionId)throw Error('Расширение не найдено.');}};
 const post=async(url,data,isForm=false)=>{
  const address=new URL(url,location.href);if(address.origin!==location.origin)throw Error('Неверный адрес очереди.');
  const response=await fetch(address.href,{method:'POST',credentials:'same-origin',headers:{'X-CSRFToken':csrf,...(isForm?{}:{'Content-Type':'application/json'})},body:isForm?data:JSON.stringify(data)});
  if(response.redirected)throw Error('Войдите в приложение и откройте очередь заново.');
  const value=await response.json();if(!response.ok||value.error)throw Error(value.error||'Данные очереди недоступны.');return value;
 };
 async function poll(){
  if(polling||busy||!rows.length)return;polling=true;
  try{
   await connect();const result=await bridge('queue-status');
   if(!result.snapshot||!result.queue_context){
    if(launchError){output.textContent=launchError;return;}
    const unfinished=rows.filter(r=>['ready','running','waiting'].includes(r.dataset.state));
    for(const row of unfinished){
     row.querySelector('.queue-state').textContent='Запуск не подтверждён браузером';
     row.querySelector('.queue-detail').textContent='Проверьте существующую заявку на Госзакупе перед продолжением. Последнее состояние сохранено в очереди.';
    }
    output.textContent=unfinished.length?'Связь с расширением установлена. Проверьте незавершённый лот перед продолжением.':'Расширение подключено. Нажмите «Запустить очередь».';
    return;
   }
   const row=rows.find(r=>r.dataset.queueId===String(result.queue_context.id)&&r.dataset.generation===result.queue_context.generation);
   if(!row)return;
   if(!stopping&&['running','waiting'].includes(result.snapshot.status)){automatic=true;activeRow=row;controls();}
   if(launchError){output.textContent=launchError;return;}
   const identity=JSON.stringify([result.queue_context,result.snapshot]);if(identity===last){output.textContent=result.snapshot.status==='complete'?'Черновик готов, результат сохранён.':'Расширение подключено. '+(result.snapshot.message||'');return;}
   const saved=await post(row.dataset.progressUrl,{generation:row.dataset.generation,snapshot:result.snapshot});
   if(saved.state==='cancelled'){await bridge('queue-stop');activeRow=null;row.hidden=true;}
   last=identity;row.dataset.state=saved.state;row.querySelector('.queue-state').textContent=saved.label;
   row.querySelector('.queue-detail').textContent=result.snapshot.message||'';
   const steps=row.querySelector('.queue-step-progress');if(steps&&saved.steps_html)steps.innerHTML=saved.steps_html;
   if(saved.workflow_stage==='awaiting_results'&&row.closest('#collection-workspace')?.dataset.stage==='preparing'&&row.dataset.movedToResults!=='true'){
    row.dataset.movedToResults='true';row.hidden=true;
    for(const link of document.querySelectorAll('.collection-funnel a')){
     const stage=new URL(link.href,location.href).searchParams.get('stage'),counter=link.querySelector('strong');
     if(counter&&['preparing','awaiting_results'].includes(stage))counter.textContent=Math.max(0,Number(counter.textContent)+(stage==='preparing'?-1:1));
    }
    output.textContent='Черновик готов. Лот перенесён в «Ожидаем итоги».';
   }
   const remove=row.querySelector('.queue-remove');if(remove)remove.disabled=['running','waiting'].includes(saved.state);
   controls();
   if(automatic&&row===activeRow&&['blocked','paused'].includes(saved.state)){automatic=false;activeRow=null;controls();}
   output.textContent=saved.workflow_stage==='awaiting_results'?'Черновик готов. Лот находится в «Ожидаем итоги».':saved.state==='complete'?'Черновик готов, результат сохранён в лоте. Можно передать следующий лот.':'Прогресс сохранён в очереди.';
  }catch(error){automatic=false;controls();output.textContent=error.message||'Не удалось обновить очередь.';}
  finally{polling=false;if(automatic&&(!activeRow||['complete','cancelled'].includes(activeRow.dataset.state)))void next();}
 }
 async function launch(row,autoStart){
  if(busy)throw Error('Дождитесь завершения передачи.');
  if(!canLaunch(row))throw Error(row.dataset.startReason||'Сначала заполните подготовку лота.');
  launchError='';busy=true;controls();const button=row.querySelector('.queue-start');if(button)button.disabled=true;
  try{
   await connect();
   if(autoStart&&!autoSupported)throw Error('Для автоматической очереди обновите расширение до 0.10.0 и перезагрузите эту страницу.');
   const current=await bridge('queue-status');
   if(['running','waiting'].includes(current.snapshot?.status))throw Error('Сначала завершите или остановите текущий лот в расширении.');
   const data=new FormData();
   const preparation=await post(row.dataset.packetUrl,data,true);
   if(preparation.skipped){row.dataset.state='cancelled';row.querySelector('.queue-state').textContent='Приём завершён';row.querySelector('.queue-detail').textContent=preparation.message;row.hidden=true;return;}
   if(preparation.packet?.reset_absent_application&&!resetSupported)throw Error('Для повторной подготовки обновите расширение до 0.13.0 и откройте очередь заново.');
   if(autoStart&&!automatic)return;
   const sent=await bridge('prepare',{...preparation,auto_start:autoStart});
   if(!sent.prepared)throw Error('Данные не переданы.');
   if(autoStart){activeRow=row;output.textContent='Очередь запущена. Создаём черновик; оставьте эту страницу открытой.';}
   else output.textContent='Объявление открыто. На его вкладке откройте расширение и нажмите «Создать / продолжить черновик».';
  }finally{busy=false;controls();}
 }
 async function next(){
  if(!automatic||busy||polling)return;
  const row=rows.find(canLaunch);
  if(!row){automatic=false;activeRow=null;controls();output.textContent=rows.some(r=>!['complete','cancelled'].includes(r.dataset.state))?'Готовых к запуску лотов нет. Заполните данные в «Подготовке».':'Очередь завершена. Результаты всех черновиков сохранены.';return;}
  try{await launch(row,true);if(row.dataset.state==='cancelled')void next();}catch(error){automatic=false;controls();launchError=error.message;output.textContent=launchError;}
 }
 runButton?.addEventListener('click',()=>{if(automatic||busy||stopping)return;automatic=true;activeRow=null;controls();if(!polling)void next();});
 stopButton?.addEventListener('click',async()=>{
  automatic=false;activeRow=null;stopping=true;controls();
  try{for(let i=0;(busy||polling)&&i<150;i++)await new Promise(resolve=>setTimeout(resolve,100));await connect();await bridge('queue-stop');output.textContent='Очередь остановлена. Текущий шаг может завершиться; следующие лоты не запускаются.';}
  catch(error){output.textContent=error.message;}
  finally{stopping=false;controls();}
 });
 for(const row of rows){
  row.querySelector('.queue-start')?.addEventListener('click',async()=>{
   if(automatic||busy||stopping)return;
   try{await launch(row,false);void poll();}catch(error){launchError=error.message;output.textContent=launchError;}
  });
 }
 checkButton?.addEventListener('click',async()=>{
  if(automatic||busy||stopping||polling)return;
  busy=true;controls();launchError='';
  try{
   await connect();
   if(!checkSupported)throw Error('Для проверки черновиков обновите расширение до 0.12.0 и откройте страницу очереди заново.');
   for(let index=0;index<rows.length;index++){
    const row=rows[index];output.textContent='Проверяем черновики: '+(index+1)+' из '+rows.length;
    const node=row.querySelector('.queue-check-result');
    try{
     const prepared=await post(row.dataset.checkUrl,new FormData(),true);
     let result;
     try{result=(await bridge('queue-check',{target:prepared.target})).result;if(!result)throw Error('Расширение не вернуло результат.');}
     catch(error){result={...prepared.target,check_state:'unknown',observed_at:new Date().toISOString(),message:error.message};}
     const saved=await post(row.dataset.checkUrl,{generation:prepared.generation,result});
     const restart=row.querySelector('.queue-restart');if(restart)restart.hidden=result.check_state!=='absent';
     node.textContent=saved.label+' · '+new Date().toLocaleTimeString()+' · '+(result.message||'');
    }catch(error){node.textContent=error.message;}
   }
   launchError='Проверка черновиков завершена. Результаты указаны в строках. Автоматическое создание не выполнялось.';output.textContent=launchError;
  }catch(error){launchError=error.message;output.textContent=launchError;}
  finally{busy=false;controls();}
 });
 controls();void poll();setInterval(()=>void poll(),4000);
})();
