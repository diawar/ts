let statusReport;
let displayedDraftKey;
const show=(value,tone='info')=>{
 const card=document.querySelector('#result-card');
 card.hidden=false;
 let title='Результат',summary=typeof value==='string'?value:'Подробности доступны ниже.';
 if(value&&typeof value==='object'){
  const titles={draft:'Черновик найден',submitted:'Заявка подана',completed:'Заявка завершена',unknown:'Статус не подтверждён'};
  if(value.state){
   title=titles[value.state]||'Статус не подтверждён';tone=['draft','submitted','completed'].includes(value.state)?'success':'warning';
   summary=value.state==='unknown'?'Проверьте фильтры и наличие нужного лота в списке. Это не означает отсутствие заявки.':
    'Заявка № '+value.number+(value.state==='completed'?'. Результат участия проверяется отдельно.':'. По данным открытой страницы.');
  }else if(value.error){title='Не удалось проверить';summary=value.error;tone='error';}
  else if(value.message){summary=value.message;}
 }
 if(tone==='error')title='Не удалось выполнить';
 card.dataset.tone=tone;
 document.querySelector('#result-title').textContent=title;
 document.querySelector('#result-summary').textContent=summary;
 document.querySelector('#result-mark').textContent=({success:'✓',warning:'!',error:'!'}[tone]||'i');
};
document.querySelector('#status').onclick=async()=>{
 statusReport=null;document.querySelector('#send').disabled=true;
 document.querySelector('#status').disabled=true;
 document.querySelector('#status-label').textContent='Проверяем…';
 let validTarget=false,semanticFailure=false,target;
 try{
  target=statusTarget(Object.fromEntries(['company_bin','announcement_number','lot_number'].map(key=>[key,document.querySelector('#'+key).value])));
  const [tab]=await chrome.tabs.query({active:true,currentWindow:true});
  const url=new URL(tab.url);if(url.origin!=='https://zakup.gov.kz'||!url.pathname.startsWith('/cabinet/'))throw Error('Откройте кабинет zakup.gov.kz');
  validTarget=true;
  const results=await chrome.scripting.executeScript({target:{tabId:tab.id},func:readPortalStatus,args:[target]});
  const report=results[0].result;if(report.error){semanticFailure=true;throw Error(report.error);}
  statusReport=report;document.querySelector('#send').disabled=false;show(report);
 }catch(error){
  if(validTarget&&!semanticFailure&&target){statusReport={schema:'goszakup-status-v1',...target,observed_at:new Date().toISOString(),state:'unknown'};document.querySelector('#send').disabled=false;}
  show(error.message,'error');
 }finally{
  document.querySelector('#status').disabled=false;
  document.querySelector('#status-label').textContent='Проверить статус';
 }
};
for(const key of ['company_bin','announcement_number','lot_number'])document.querySelector('#'+key).addEventListener('input',()=>{
 statusReport=null;document.querySelector('#send').disabled=true;show('Условия изменены. Нажмите «Проверить статус».');
});
document.querySelector('#send').onclick=async()=>{
 if(!statusReport)return;
 try{
  const url=reportTransferUrl(document.querySelector('#app-url').value.trim(),statusReport);
  await chrome.tabs.create({url});
  show('Открыта передача в приложение. Результат сохранения проверяйте в новой вкладке.');
 }catch(error){show(error.message,'error');}
};

let absenceTarget;
function renderDraft(job){
 absenceTarget=job?.packet;
 const absent=document.querySelector('#draft-confirm-absent');
 absent.hidden=!(job?.createUncertain&&!job.number);
 absent.disabled=!['paused','blocked','ready'].includes(job?.status);
 const active=['running','waiting'].includes(job?.status);
 document.querySelector('#draft-state').textContent=job?
  'Лот '+job.packet.lot_number+' · Объявление '+job.packet.announcement_number+(job.number?' · Черновик № '+job.number:'')+' · '+job.message:'Подготовка не загружена.';
 document.querySelector('#draft-start').disabled=!job||active;
 document.querySelector('#draft-stop').disabled=!active;
 document.querySelector('#draft-clear').disabled=!job||active;
 document.querySelector('#status').disabled=active;
 if(!job){
  displayedDraftKey=undefined;statusReport=null;
  for(const name of ['company_bin','announcement_number','lot_number'])document.querySelector('#'+name).value='';
  document.querySelector('#send').disabled=true;document.querySelector('#result-card').hidden=true;
  return;
 }
 if(job?.packet){
  const packet=job.packet;
  const key=[packet.company_bin,packet.announcement_number,packet.lot_number,job.appOrigin||'',job.appUserId||''].join(':');
  if(displayedDraftKey!==key){
   displayedDraftKey=key;
   for(const name of ['company_bin','announcement_number','lot_number'])document.querySelector('#'+name).value=packet[name]||'';
   if(job.appOrigin)document.querySelector('#app-url').value=job.appOrigin;
  }
 }
 if(job?.number&&job.lotConfirmed&&!['running','waiting'].includes(job.status)){
  statusReport={schema:'goszakup-status-v1',company_bin:job.packet.company_bin,
   announcement_number:job.packet.announcement_number,lot_number:job.packet.lot_number,
   state:'draft',description_saved:job.status==='complete',number:job.number,url:'https://zakup.gov.kz/cabinet/purchase/my-applications/'+job.number,
   observed_at:job.observedAt||new Date().toISOString()};
  document.querySelector('#send').disabled=false;
  show(statusReport);
  if(['blocked','paused'].includes(job.status)){
   document.querySelector('#result-title').textContent='Черновик найден, заполнение остановлено';
   document.querySelector('#result-summary').textContent='Заявка № '+job.number+'. '+job.message;
   document.querySelector('#result-card').dataset.tone='warning';
  }else if(job.status==='complete'){
   document.querySelector('#result-title').textContent='Описание товара заполнено';
   document.querySelector('#result-summary').textContent='Заявка № '+job.number+'. '+job.message;
  }
 }
}
let checkingDraft=false;
async function draftCommand(type){
 if(type==='draft:get'&&checkingDraft)return;
 if(type==='draft:get')checkingDraft=true;
 try{const result=await chrome.runtime.sendMessage({type});if(result.error)throw Error(result.error);renderDraft(result.job);}
 catch(error){renderDraft(null);show(error.message,'error');}
 finally{if(type==='draft:get')checkingDraft=false;}
}
document.querySelector('#draft-start').onclick=()=>draftCommand('draft:start');
document.querySelector('#draft-confirm-absent').onclick=async()=>{
 try{const result=await chrome.runtime.sendMessage({type:'draft:confirm-absent',company_bin:absenceTarget?.company_bin,announcement_number:absenceTarget?.announcement_number,lot_number:absenceTarget?.lot_number});if(result.error)throw Error(result.error);renderDraft(result.job);}
 catch(error){show(error.message,'error');}
};

document.querySelector('#draft-stop').onclick=()=>draftCommand('draft:stop');
document.querySelector('#draft-clear').onclick=async()=>{
 await draftCommand('draft:clear');statusReport=null;displayedDraftKey=undefined;
 document.querySelector('#send').disabled=true;
};
if(chrome.runtime?.sendMessage){
 void draftCommand('draft:get');
 chrome.storage.onChanged.addListener((changes,area)=>{if(area==='session'&&changes.draftJob){renderDraft(null);void draftCommand('draft:get');}});
 setInterval(()=>{if(displayedDraftKey)void draftCommand('draft:get');},3000);
}
