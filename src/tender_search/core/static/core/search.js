function tagIcon(name){const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.classList.add('icon');svg.setAttribute('aria-hidden','true');const use=document.createElementNS('http://www.w3.org/2000/svg','use');use.setAttribute('href',document.body.dataset.icons+'#'+name);svg.append(use);return svg;}

// Rename a saved collection independently of the current search form.
function closeProfileRename(row){
 const form=row.querySelector('[data-profile-rename]');
 row.classList.remove('is-renaming');
 form.hidden=true;
 form.elements.namedItem('name').value=row.querySelector('[data-profile-label]').textContent;
 row.querySelector('[data-profile-title]').hidden=false;
 row.querySelector('[data-profile-error]').hidden=true;
 form.elements.namedItem('name').removeAttribute('aria-invalid');
 row.querySelector('[data-profile-edit]').focus({preventScroll:true});
}
function profileRenameError(form,message){
 const error=form.querySelector('[data-profile-error]');
 error.textContent=message;error.hidden=false;
 form.elements.namedItem('name').setAttribute('aria-invalid','true');
}
document.addEventListener('click',event=>{
 const edit=event.target.closest('[data-profile-edit]');
 if(edit){
  const row=edit.closest('[data-saved-profile]'),form=row.querySelector('[data-profile-rename]');
  row.classList.add('is-renaming');
  row.querySelector('[data-profile-title]').hidden=true;form.hidden=false;
  const input=form.elements.namedItem('name');input.value=row.querySelector('[data-profile-label]').textContent;input.focus();input.select();
 }
 const cancel=event.target.closest('[data-profile-cancel]');
 if(cancel&&!cancel.form.dataset.busy)closeProfileRename(cancel.closest('[data-saved-profile]'));
});
document.addEventListener('keydown',event=>{
 const form=event.target.closest('[data-profile-rename]');
 if(!form||event.isComposing)return;
 if(event.key==='Escape'){event.preventDefault();if(!form.dataset.busy)closeProfileRename(form.closest('[data-saved-profile]'));}
});
document.addEventListener('input',event=>{
 const form=event.target.closest('[data-profile-rename]');
 if(form){form.querySelector('[data-profile-error]').hidden=true;event.target.removeAttribute('aria-invalid');}
});
document.addEventListener('submit',async event=>{
 const form=event.target;
 if(!form.matches('[data-profile-rename]'))return;
 event.preventDefault();if(form.dataset.busy)return;
 const row=form.closest('[data-saved-profile]'),label=row.querySelector('[data-profile-label]'),input=form.elements.namedItem('name');
 const name=input.value.trim(),oldName=label.textContent;
 if(!name){profileRenameError(form,'Введите название подборки.');input.focus();return;}
 if(name.length>200){profileRenameError(form,'Название должно быть не длиннее 200 символов.');input.focus();return;}
 if(name===oldName){closeProfileRename(row);return;}
 form.dataset.busy='true';form.setAttribute('aria-busy','true');input.readOnly=true;
 form.querySelectorAll('button').forEach(button=>{button.disabled=true;});
 const failure='Не удалось сохранить название. Попробуйте ещё раз.';
 try{
  const data=new FormData(form);data.set('name',name);
  const response=await fetch(form.action,{method:'POST',body:data,credentials:'same-origin',headers:{Accept:'application/json'}});
  const result=await response.json();
  if(!response.ok||typeof result.name!=='string'){profileRenameError(form,result.error||failure);return;}
  label.textContent=result.name;
  row.querySelectorAll('[aria-label],[title]').forEach(node=>{
   for(const attr of ['aria-label','title']){
    const value=node.getAttribute(attr);
    if(value?.endsWith(oldName))node.setAttribute(attr,value.slice(0,-oldName.length)+result.name);
   }
  });
  closeProfileRename(row);
 }catch(error){profileRenameError(form,failure);}
 finally{
  delete form.dataset.busy;form.removeAttribute('aria-busy');input.readOnly=false;
  form.querySelectorAll('button').forEach(button=>{button.disabled=false;});
 }
});
// The server supplies elapsed time, avoiding dependence on the browser clock.
function updateIndexTimer(){
 const timer=document.querySelector('[data-index-elapsed]');
 if(!timer)return;
 if(timer._timerAnchor===undefined)timer._timerAnchor=performance.now();
 const elapsed=Number(timer.dataset.indexElapsed)+(timer.dataset.running==='true'?Math.floor((performance.now()-timer._timerAnchor)/1000):0);
 timer.textContent=[Math.floor(elapsed/3600),Math.floor(elapsed/60)%60,elapsed%60].map(n=>String(n).padStart(2,'0')).join(':');
}
updateIndexTimer();
setInterval(updateIndexTimer,1000);
document.addEventListener('htmx:afterSwap',updateIndexTimer);
function splitSearchTags(value,kind='contains'){
 return [...new Set(value.split(kind==='phrase'?/[\n\r\t;,]+/:/[\s;,]+/).map(s=>s.trim().replace(/\s+/g,' ')).filter(Boolean))];
}
document.querySelectorAll('[data-tags]').forEach(editor => {
 const source=editor.querySelector('textarea'), list=editor.querySelector('.tag-list'), entry=editor.querySelector('.tag-entry');
 const form=editor.closest('form');
 const phraseMode=()=>editor.dataset.tags==='terms'&&form.elements.namedItem('kind')?.value==='phrase';
 const parse=value=>splitSearchTags(value,phraseMode()?'phrase':'contains');
 const delimiter=(key,value)=>phraseMode()?(['Enter',';',','].includes(key)||(key==='Tab'&&value.trim())):['Enter',';',' ', ','].includes(key);
 function updateEntry(){entry.placeholder=phraseMode()?'Введите фразу · Enter или Tab — следующая':'Введите слово и нажмите пробел';entry.setAttribute('aria-label',phraseMode()?'Добавить ключевую фразу':(editor.dataset.tags==='exclude'?'Добавить исключение':'Добавить ключевое слово'));}
 if(editor.dataset.tags==='terms')form.elements.namedItem('kind')?.addEventListener('change',()=>{tags=parse([...tags,entry.value].join('; '));entry.value='';updateEntry();render();});
 updateEntry();
 let tags=parse(source.value); source.hidden=true; source.required=false;
 editor.addEventListener('remove-term',event=>{tags=tags.filter(term=>term!==event.detail);render();});
 const sync=()=>{source.value=tags.join('; ');source.dispatchEvent(new Event('change',{bubbles:true}));};
 function render(){
  list.replaceChildren();
  tags.forEach((text,index)=>{
   const chip=document.createElement('span');chip.className='tag '+(editor.dataset.tags==='exclude'?'tag-exclusion':'tag-keyword');
   const label=document.createElement('span');label.textContent=text;
   const edit=document.createElement('button');edit.type='button';edit.className='tag-edit';edit.append(tagIcon('pencil'));edit.setAttribute('aria-label','Изменить '+text);
   edit.onclick=()=>{
    const input=document.createElement('input');input.value=text;input.maxLength=150;input.className='tag-inline';chip.replaceChildren(input);
    let done=false;
    const finish=()=>{if(done)return;done=true;tags.splice(index,1,...parse(input.value));tags=[...new Set(tags)];render();};
    input.onkeydown=e=>{if(!e.isComposing&&delimiter(e.key,input.value)){e.preventDefault();finish();entry.focus();}if(e.key==='Escape'){done=true;render();}};
    input.onblur=finish;input.focus();input.select();
   };
   const remove=document.createElement('button');remove.type='button';remove.append(tagIcon('x'));remove.setAttribute('aria-label','Удалить '+text);
   remove.onclick=()=>{tags.splice(index,1);render();};chip.append(label,edit,remove);list.append(chip);
  });sync();
 }
 function add(){
  const values=parse(entry.value);
  if(values.some(s=>s.length>150)){entry.setCustomValidity('Тег не должен быть длиннее 150 символов.');entry.reportValidity();return false;}
  tags=[...new Set([...tags,...values])];entry.value='';entry.setCustomValidity('');render();return true;
 }
 entry.addEventListener('keydown',e=>{if(!e.isComposing&&delimiter(e.key,entry.value)){e.preventDefault();add();}});
 entry.addEventListener('input',()=>entry.setCustomValidity(''));
 entry.addEventListener('blur',()=>{if(entry.value.trim())add();});
 form.addEventListener('submit',e=>{if(!add())e.preventDefault();});
 render();
});
document.querySelectorAll('.procurement-group').forEach(group=>{
 const toggle=group.querySelector('[data-group-toggle]'), boxes=[...group.querySelectorAll('.procurement-children input[type="checkbox"]')];
 const sync=()=>{const count=boxes.filter(box=>box.checked).length;toggle.checked=count===boxes.length&&count>0;toggle.indeterminate=count>0&&count<boxes.length;};
 toggle.addEventListener('click',event=>event.stopPropagation());
 toggle.addEventListener('change',()=>{const checked=toggle.checked;boxes.forEach(box=>{box.checked=checked;box.dispatchEvent(new Event('change',{bubbles:true}));});sync();});
 boxes.forEach(box=>box.addEventListener('change',sync));sync();
});
// Both summaries use the same grouping, while the form still submits source IDs.
function selectedFilterItems(root,field){
 const boxes=[...root.querySelectorAll('input[name="'+field+'"]')], handled=new Set(), items=[];
 for(const box of boxes){
  if(!box.checked||handled.has(box))continue;
  const group=box.closest('.procurement-group');
  const members=group?boxes.filter(candidate=>candidate.closest('.procurement-group')===group):[];
  if(group&&members.length&&members.every(candidate=>candidate.checked)){
   members.forEach(candidate=>handled.add(candidate));
   items.push({label:group.dataset.groupLabel,boxes:members});
  }else{handled.add(box);items.push({label:box.closest('label').textContent.trim(),boxes:[box]});}
 }
 return items;
}
function clearFilterBoxes(boxes){
 boxes.forEach(box=>{box.checked=false;});
 boxes.forEach(box=>box.dispatchEvent(new Event('change',{bubbles:true})));
}
for (const field of ['region','city','status','method']) {
 const boxes=[...document.querySelectorAll('input[name="'+field+'"]')];
 const counter=document.querySelector('[data-count="'+field+'"]');
 const update=()=>{const count=boxes.filter(b=>b.checked).length;if(counter)counter.textContent=count?'('+count+')':'';const preview=document.querySelector('[data-preview="'+field+'"]');if(preview){preview.replaceChildren();selectedFilterItems(document,field).forEach(item=>{const chip=document.createElement('span');chip.className='selected-filter';const label=document.createElement('span');label.textContent=item.label;const remove=document.createElement('button');remove.type='button';remove.className='remove-filter';remove.setAttribute('aria-label','Убрать условие: '+item.label);remove.append(tagIcon('x'));remove.addEventListener('click',()=>clearFilterBoxes(item.boxes));chip.append(label,remove);preview.append(chip);});}};
 boxes.forEach(b=>b.addEventListener('change',update));update();
}

const dropdowns=[...document.querySelectorAll('.check-dropdown')];
document.addEventListener('click',event=>{dropdowns.forEach(dropdown=>{if(!dropdown.contains(event.target))dropdown.open=false;});});
document.addEventListener('keydown',event=>{if(event.key==='Escape'){dropdowns.forEach(dropdown=>dropdown.open=false);}});
document.addEventListener('focusin',event=>{dropdowns.forEach(dropdown=>{if(!dropdown.contains(event.target))dropdown.open=false;});});

const profileForm=document.querySelector('#procurement-search-form');
if(profileForm){
 const save=profileForm.querySelector('button[value="save"]');
 function signature(){
  const values=[...new FormData(profileForm).entries()].filter(([key])=>!['csrfmiddlewaretoken','profile_id','action'].includes(key));
  profileForm.querySelectorAll('.tag-entry,.tag-inline').forEach((input,index)=>{if(input.value.trim())values.push(['pending-'+index,input.value.trim()]);});
  return JSON.stringify(values);
 }
 const savedSignature=profileForm.dataset.saved==='true'?signature():null;
 const updateSave=()=>{if(save)save.disabled=profileForm.dataset.saveAllowed!=='true'||(savedSignature!==null&&signature()===savedSignature);};
 profileForm.addEventListener('input',updateSave);profileForm.addEventListener('change',updateSave);profileForm.addEventListener('click',()=>queueMicrotask(updateSave));updateSave();
}

document.addEventListener('click',event=>{
 const button=event.target.closest('[data-condition-field]');if(!button)return;
 const form=document.querySelector('#procurement-search-form');if(!form)return;
 const field=button.dataset.conditionField,value=button.dataset.conditionValue;
 if(field==='subject'||field==='remaining_hours'){
  const all=form.querySelector('input[name="'+field+'"][value=""]');
  if(all){all.checked=true;all.dispatchEvent(new Event('change',{bubbles:true}));}
  const hint=document.querySelector('.conditions-changed');if(hint)hint.hidden=false;
  return;
 }
 if(['terms','exclude'].includes(field)){
  form.querySelector('[data-tags="'+field+'"]').dispatchEvent(new CustomEvent('remove-term',{detail:value}));
 }else if(['status','method','region','city'].includes(field)){
  const values=button.dataset.conditionValues?JSON.parse(button.dataset.conditionValues):null;
  clearFilterBoxes([...form.querySelectorAll('input[name="'+field+'"]')].filter(box=>values?values.includes(box.value):box.closest('label').textContent.trim()===value));
 }else{
  const input=form.elements.namedItem(field);if(input){if(input.type==='checkbox')input.checked=false;else input.value=['mode','kind','depth'].includes(field)?value:'';input.dispatchEvent(new Event('change',{bubbles:true}));}
 }
 button.closest('.condition-chip').remove();const hint=document.querySelector('.conditions-changed');if(hint)hint.hidden=false;
});

// Interval is adjusted with native spinner arrows only; preserve mouse spinner controls.
document.addEventListener('keydown',event=>{
 if(!event.target.matches('.profile-notifications input[name="hours"]'))return;
 if(!['ArrowUp','ArrowDown','Tab','Escape'].includes(event.key))event.preventDefault();
});
for(const eventName of ['paste','drop','cut']){
 document.addEventListener(eventName,event=>{if(event.target.matches('.profile-notifications input[name="hours"]'))event.preventDefault();});
}

document.addEventListener('click',event=>{
 const button=event.target.closest('[data-hours-step]');if(!button)return;
 const form=button.closest('.profile-notifications'), input=form.querySelector('input[name="hours"]');
 const value=Math.min(12,Math.max(1,Math.round(Number(input.value)||1)+Number(button.dataset.hoursStep)));
 input.value=String(value);form.querySelector('.interval-value').textContent=String(value);
 input.dispatchEvent(new Event('change',{bubbles:true}));
});

const conditionsForm=document.querySelector('#procurement-search-form');
const liveConditions=document.querySelector('#selected-conditions .conditions-live');
if(conditionsForm&&liveConditions){
 function renderConditions(){
  liveConditions.replaceChildren();
  const value=name=>conditionsForm.elements.namedItem(name)?.value||'';
  const add=(label,field,val,tone,removable=true,selectedValues=null)=>{
   const chip=document.createElement('span');chip.className='condition-chip '+tone;
   const sign=document.createElement('b');sign.textContent=field==='exclude'?'−':'+';
   const text=document.createElement('span');text.textContent=label;chip.append(sign,text);
   if(removable){const button=document.createElement('button');button.type='button';button.className='remove-filter';button.dataset.conditionField=field;button.dataset.conditionValue=val;if(selectedValues)button.dataset.conditionValues=JSON.stringify(selectedValues);button.setAttribute('aria-label','Убрать условие: '+label);button.append(tagIcon('x'));chip.append(button);}
   liveConditions.append(chip);
  };
  const words=name=>splitSearchTags(value(name),name==='terms'?value('kind'):'contains');
  words('terms').forEach(word=>add(word,'terms',word,'condition-keyword'));
  for(const [name,label] of [['amount_min','Сумма от'],['amount_max','Сумма до'],['customer_bin','БИН заказчика']]){if(value(name))add(label+': '+value(name)+(name.startsWith('amount')?' ₸':''),name,value(name),'condition-procurement');}
  for(const [name,label] of [['status','Статус'],['method','Способ закупки'],['region','Область'],['city','Город']]){
   selectedFilterItems(conditionsForm,name).forEach(item=>add(label+': '+item.label,name,item.label,'condition-procurement',true,item.boxes.map(box=>box.value)));
  }
  if(conditionsForm.elements.namedItem('only_advance')?.checked)add('Только с авансом','only_advance','on','condition-procurement');
  if(conditionsForm.elements.namedItem('only_medical')?.checked)add('Закупки по медицине (med.ecc.kz, fms.ecc.kz)','only_medical','on','condition-procurement');
  if(value('remaining_hours'))add('До окончания приёма: '+value('remaining_hours')+' ч','remaining_hours',value('remaining_hours'),'condition-procurement');
  if(value('subject'))add('Предмет закупки: '+({'1':'Товары','2':'Работы','3':'Услуги'}[value('subject')]),'subject',value('subject'),'condition-procurement');
  for(const [name,label] of [['deadline_from','Окончание приёма от'],['deadline_to','Окончание приёма до']]){if(value(name))add(label+': '+value(name).split('-').reverse().join('.'),name,value(name),'condition-procurement');}
  if(value('number'))add((value('number_kind')==='announcement_number'?'Номер объявления: ':'Номер лота: ')+value('number'),'number',value('number'),'condition-procurement');
  words('exclude').forEach(word=>add(word,'exclude',word,'condition-exclude'));
 }
 conditionsForm.addEventListener('input',renderConditions);conditionsForm.addEventListener('change',renderConditions);renderConditions();
}

function setSearchBusy(busy, allowCancel=true){
 document.querySelectorAll('button[type="submit"][name="action"][value="search"], button.conditions-search').forEach(button=>{
  const cancelUrl=document.querySelector('#search-results')?.dataset.cancelUrl;
  if(button.classList.contains('conditions-search')){
   if(!button.dataset.searchMarkup)button.dataset.searchMarkup=button.innerHTML;
   const cancelling=busy&&allowCancel&&Boolean(cancelUrl);
   button.value=cancelling?'cancel':'search';button.classList.toggle('cancel-search',cancelling);
   button.title=cancelling?'Отменить поиск':'Запустить поиск';button.setAttribute('aria-label',button.title);
   if(cancelling){button.setAttribute('formaction',cancelUrl);button.setAttribute('formnovalidate','');button.innerHTML='<svg class="icon" aria-hidden="true"><use href="'+document.body.dataset.icons+'#x"></use></svg>';button.disabled=false;return;}
   button.removeAttribute('formaction');button.removeAttribute('formnovalidate');button.innerHTML=button.dataset.searchMarkup;
  }
  button.disabled=busy;button.classList.toggle('is-searching',busy);button.setAttribute('aria-busy',String(busy));
  const ring=button.querySelector('.button-spinner');
  if(busy&&!ring){const spinner=document.createElement('span');spinner.className='search-spinner button-spinner';spinner.setAttribute('aria-hidden','true');button.prepend(spinner);}
  if(!busy&&ring)ring.remove();
 });
}
function syncSearchBusy(){setSearchBusy(document.querySelector('#search-results')?.dataset.searchPending==='true');}
// The browser reads the submitter's formaction after submit listeners finish.
// Do not redirect a new search to the previous job's cancellation endpoint.
if(conditionsForm){conditionsForm.addEventListener('submit',event=>{if(!event.defaultPrevented&&event.submitter?.value==='search')setSearchBusy(true,false);});}
document.addEventListener('htmx:afterSwap',syncSearchBusy);
window.addEventListener('pageshow',syncSearchBusy);
syncSearchBusy();

// Do not discard a workflow edit when the saved collection refreshes itself.
document.addEventListener('input',event=>{
 const form=event.target.form;if(form?.matches('.collection-stage-form, .collection-note-form, .lot-note-form, .lot-price-form'))form.dataset.dirty='true';
});
document.addEventListener('change',event=>{
 const form=event.target.form;if(form?.matches('.collection-stage-form, .collection-note-form, .lot-note-form, .lot-price-form'))form.dataset.dirty='true';
});
document.addEventListener('htmx:beforeRequest',event=>{
 if(event.detail.elt?.id==='lot-detail'){
  if(document.querySelector('.lot-note-form[data-dirty="true"], .lot-price-form[data-dirty="true"]')||document.activeElement?.form?.matches('.lot-note-form, .lot-price-form'))event.preventDefault();
  return;
 }
 if(event.detail.elt?.id!=='collection-workspace')return;
 if(document.querySelector('.collection-stage-form[data-dirty="true"], .collection-note-form[data-dirty="true"], .lot-note-form[data-dirty="true"], .lot-price-form[data-dirty="true"]')||document.activeElement?.form?.matches('.collection-stage-form, .collection-note-form, .lot-note-form, .lot-price-form'))event.preventDefault();
});

// Refresh only personal controls: keep filters, workflow edits and notes intact.
(() => {
 const owner=document.body.dataset.userId;
 if(!owner)return;
 let busy=false,again=false,timer,forceNext=false,lastSync=Date.now(),workspaceInFlight=false,workspaceMutation=false;
 async function syncLotState(force=false){
  if(document.hidden)return;
  // A full workspace refresh already includes the same flags and counters.
  // Newly rendered controls are fresh too; only a mutation bypasses this gate.
  if(!force&&(document.querySelector('#collection-workspace[data-background-poll]')||Date.now()-lastSync<5000))return;
  if(busy){again=true;forceNext=forceNext||force;return;}
  const ids=[...new Set([...document.querySelectorAll('.lot-actions[data-lot-id]')].map(el=>el.dataset.lotId))];
  if(!ids.length)return;
  busy=true;
  try{
   for(let offset=0;offset<ids.length;offset+=100){
    const response=await fetch(document.body.dataset.lotStateUrl+'?ids='+ids.slice(offset,offset+100).join(','),{credentials:'same-origin',cache:'no-store',signal:AbortSignal.timeout(20000)});
    if(!response.ok)continue;
    const state=await response.json();if(state.owner!==owner)return;
    const count=document.querySelector('#sidebar-favorite-count');if(count)count.textContent='('+state.favorite_count+')';
    const collectionCount=document.querySelector('#sidebar-collection-count');if(collectionCount&&Number.isInteger(state.collection_count))collectionCount.textContent='('+state.collection_count+')';
    for(const lot of state.lots){
     document.querySelectorAll('.lot-actions[data-lot-id="'+lot.id+'"]').forEach(actions=>{
      for(const [selector,selected,on,off] of [['.favorite-icon',lot.favorite,'remove','add'],['.status-watch-icon',lot.watched,'off','on']]){
       const button=actions.querySelector(selector);if(!button)continue;
       button.classList.toggle('selected',selected);button.setAttribute('aria-pressed',String(selected));
       const input=button.form?.elements.namedItem('action');if(input)input.value=selected?on:off;
       if(selector==='.favorite-icon'){
        button.disabled=(state.locked_lots||[]).includes(lot.id);
        button.title=button.disabled?'Лот в работе — звезда закреплена за этапом':(selected?'Убрать из избранного':'В избранное');
       }else{
        button.disabled=true;
        button.title='Уведомления '+(selected?'включены':'выключены')+' · определяются этапом лота';
       }
       button.setAttribute('aria-label',button.title+': '+actions.dataset.lotNumber);
      }
     });
    }
    lastSync=Date.now();
   }
  }catch(error){/* Leave the last confirmed state on a transient network error. */}
  finally{busy=false;if(again){again=false;schedule();}}
 }
 function schedule(force=false){
  forceNext=forceNext||force===true;
  clearTimeout(timer);
  if(document.hidden)return;
  timer=setTimeout(()=>{const force=forceNext;forceNext=false;void syncLotState(force);},150);
 }
 const channel=typeof BroadcastChannel==='function'?new BroadcastChannel('lot-state-'+owner):null;
 if(channel)channel.onmessage=()=>{if(workspaceInFlight)workspaceMutation=true;schedule(true);};
 document.addEventListener('htmx:beforeSend',event=>{
  if(event.detail.elt?.id==='collection-workspace'){workspaceInFlight=true;workspaceMutation=false;}
 });
 document.addEventListener('htmx:afterRequest',event=>{
  if(event.detail.elt?.id==='collection-workspace')workspaceInFlight=false;
  const path=event.detail.requestConfig?.path||'';
  if(event.detail.successful&&/\/lots\/\d+\/(favorite|status-watch)\//.test(path)){if(workspaceInFlight)workspaceMutation=true;schedule(true);channel?.postMessage('changed');}
 });
 document.addEventListener('htmx:afterSwap',event=>{
  const workspace=event.detail.elt;
  if(workspace?.id!=='collection-workspace')return;
  for(const [key,selector] of [['collectionCount','#sidebar-collection-count'],['favoriteCount','#sidebar-favorite-count']]){
   const value=workspace.dataset[key],node=document.querySelector(selector);
   if(node&&/^\d+$/.test(value||''))node.textContent='('+value+')';
  }
  lastSync=Date.now();
  // A concurrent mutation can be newer than the table response we just swapped.
  if(workspaceMutation){workspaceMutation=false;schedule(true);}
 });
 window.addEventListener('pageshow',event=>{if(event.persisted)schedule(true);});
 window.addEventListener('focus',()=>schedule());
 document.addEventListener('visibilitychange',()=>schedule());
 setInterval(()=>schedule(),30000);
})();

document.addEventListener("change",event=>{if(event.target.matches("select[data-page-size]"))event.target.form.requestSubmit();});

// HTMX replaces pending results frequently; derive the frame from search time,
// so polling does not restart the illustration cycle or introduce layout jumps.
(()=>{
 const reduced=window.matchMedia('(prefers-reduced-motion: reduce)');
 function updateSearchArt(){
  document.querySelectorAll('.search-progress-art').forEach(art=>{
   const frames=[...art.querySelectorAll('[data-progress-frame]')];
   const start=Number(art.dataset.searchStart)*1000;
   const frame=reduced.matches?0:Math.floor(Math.max(0,Date.now()-start)/4000)%frames.length;
   frames.forEach((image,index)=>{image.hidden=index!==frame;});
   art.querySelectorAll('.search-progress-dots i').forEach((dot,index)=>{dot.classList.toggle('active',index===frame);});
  });
 }
 updateSearchArt();
 document.addEventListener('htmx:afterSwap',updateSearchArt);
 setInterval(updateSearchArt,1000);
})();
