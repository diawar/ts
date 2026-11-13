// Private DOM and pending actions belong to the account that rendered this page.
(() => {
 const owner=document.body.dataset.userId;
 if(!owner)return;
 const originalFetch=window.fetch.bind(window),root=document.documentElement;
 let pending=null,changed=false,verified=false,revision=0,focused=true;
 function lock(){verified=false;revision++;root.dataset.accountCheck='pending';document.body.inert=true;}
 function replace(){
  if(changed)return;
  changed=true;lock();document.body.replaceChildren();
  // A GET navigation avoids re-submitting a form restored from history.
  window.location.replace(window.location.href);
 }
 async function verify(){
  if(changed||document.hidden||!focused)return false;
  if(pending)return pending;
  lock();const version=revision;
  pending=(async()=>{
   try{
    const response=await originalFetch('/account/identity/',{credentials:'same-origin',cache:'no-store',redirect:'error',signal:AbortSignal.timeout(6000)});
    if(!response.ok)throw Error('Identity unavailable');
    const account=await response.json();
    if(String(account.user_id||'')!==owner){replace();return false;}
    if(changed||document.hidden||!focused||revision!==version)return false;
    verified=true;delete root.dataset.accountCheck;document.body.inert=false;
    document.dispatchEvent(new CustomEvent('app:account-ready'));
    return true;
   }catch(error){if(!changed)root.dataset.accountCheck='error';return false;}
   finally{pending=null;if(!changed&&!document.hidden&&focused&&!verified&&revision!==version)void verify();}
  })();
  return pending;
 }
 window.TenderSearchAccount={verify,changed:replace,get ready(){return verified&&!changed;}};
 window.fetch=async(input,options)=>{
  const request=new Request(input,options);
  if(new URL(request.url).origin!==location.origin)return originalFetch(request);
  request.headers.set('X-TenderSearch-User',owner);
  const response=await originalFetch(request);
  const actual=response.headers.get('X-TenderSearch-User');
  if(changed||response.status===409||actual&&actual!==owner){replace();throw Error('Аккаунт изменился.');}
  return response;
 };
 document.addEventListener('submit',event=>{
  if(!verified||changed){event.preventDefault();event.stopImmediatePropagation();void verify();return;}
  let field=event.target.querySelector('input[name="_account_id"]');
  if(!field){field=document.createElement('input');field.type='hidden';field.name='_account_id';event.target.append(field);}
  field.value=owner;
 },true);
 document.addEventListener('htmx:configRequest',event=>{event.detail.headers['X-TenderSearch-User']=owner;});
 document.addEventListener('htmx:beforeRequest',event=>{if(!verified||changed)event.preventDefault();});
 document.addEventListener('htmx:beforeSwap',event=>{
  const xhr=event.detail.xhr,actual=xhr.getResponseHeader('X-TenderSearch-User');
  if(xhr.status===409||actual&&actual!==owner){event.preventDefault();replace();}
  else if(!verified||changed)event.preventDefault();
 });
 window.addEventListener('blur',()=>{focused=false;lock();});
 window.addEventListener('pagehide',lock);
 window.addEventListener('focus',()=>{focused=true;void verify();});
 window.addEventListener('pageshow',()=>{if(!document.hidden)focused=true;void verify();});
 document.addEventListener('visibilitychange',()=>{if(document.hidden)lock();else{focused=true;void verify();}});
 lock();void verify();
})();
