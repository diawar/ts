/* One-way preparation handoff from our application, never a portal controller. */
(()=>{
 const queuePage=location.pathname==='/workspace/drafts/';
 if(window.top!==window||!globalThis.GOSZAKUP_APP_ORIGINS?.includes(location.origin)||(!/^\/lots\/\d+\/preparation\/$/.test(location.pathname)&&!queuePage))return;
 if(globalThis.GOSZAKUP_BRIDGE_ATTACHED)return;
 globalThis.GOSZAKUP_BRIDGE_ATTACHED=true;
 const channel='goszakup-preparation-bridge-v1';
 async function identity(){
  if(document.readyState==='loading')await new Promise(resolve=>document.addEventListener('DOMContentLoaded',resolve,{once:true}));
  const user=document.body?.dataset.userId;
  if(!/^\d+$/.test(user||''))throw Error('Откройте подготовку заново после входа в приложение.');
  const response=await fetch('/account/identity/',{credentials:'same-origin',cache:'no-store',redirect:'error',signal:AbortSignal.timeout(6000)});
  if(!response.ok)throw Error('Не удалось проверить аккаунт приложения.');
  const account=await response.json();
  if(account.user_id!==user)throw Error('Аккаунт изменился. Откройте подготовку заново.');
  return {user_id:user,origin:location.origin,pathname:location.pathname};
 }
 chrome.runtime.onMessage.addListener((message,sender,respond)=>{
  if(sender.id!==chrome.runtime.id||message?.type!=='bridge:identity')return false;
  identity().then(respond,error=>respond({error:error.message}));return true;
 });
 let busy=false;
 const reply=(id,value)=>window.postMessage({channel,id,...value},location.origin);
 window.addEventListener('message',async event=>{
  const message=event.data;
  if(event.source!==window||event.origin!==location.origin||!message||message.channel!==channel||typeof message.id!=='string'||!/^[\w-]{16,80}$/.test(message.id))return;
  if(message.type==='ping'){reply(message.id,{type:'ready',extensionId:chrome.runtime.id,autoQueue:true,queueCheck:true,queueReset:true});return;}
  if(message.type!=='prepare'&&!(queuePage&&['queue-status','queue-stop','queue-check'].includes(message.type)))return;
  if(message.extensionId!==chrome.runtime.id)return;
  if(busy){reply(message.id,{type:'result',error:'Передача уже выполняется.'});return;}
  busy=true;
  try{
   const account=await identity();
   if(['queue-status','queue-stop','queue-check'].includes(message.type)){
    const result=await chrome.runtime.sendMessage({type:'bridge:'+message.type,account_id:account.user_id,...(message.type==='queue-check'?{target:message.target}:{})});
    reply(message.id,{type:'result',...result});return;
   }
   if(JSON.stringify(message.packet).length>50000)throw Error('Слишком большой пакет подготовки.');
   const result=await chrome.runtime.sendMessage({type:'bridge:prepare',account_id:account.user_id,packet:message.packet,...(queuePage?{queue_context:message.queue_context,auto_start:message.auto_start===true}:{})});
   if(result?.error)throw Error(result.error);
   if(!result?.prepared)throw Error('Расширение не подтвердило получение данных.');
   reply(message.id,{type:'result',prepared:true,opened:result.opened===true});
  }catch(error){reply(message.id,{type:'result',error:error.message||'Не удалось передать подготовку.'});}
  finally{busy=false;}
 });
})();
