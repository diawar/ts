const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const code=fs.readFileSync('src/tender_search/core/static/core/private-pages.js','utf8');
const settle=()=>new Promise(resolve=>setImmediate(resolve));
function page({offline=false,defer=false}={}){
 const docEvents={},winEvents={},root={dataset:{}},state={user:'1',offline,replaced:false,cleared:false,calls:[]};
 let finish,finishAction;
 const document={hidden:false,documentElement:root,
  body:{dataset:{userId:'1'},inert:false,replaceChildren(){state.cleared=true;}},
  addEventListener(name,fn){(docEvents[name]??=[]).push(fn);},
  dispatchEvent(event){for(const fn of docEvents[event.type]||[])fn(event);},
  createElement(){return {};}};
 const location={origin:'http://localhost:8001',href:'http://localhost:8001/lots/1/preparation/',replace(url){state.replaced=url;}};
 const response=(user,status=200)=>new Response(JSON.stringify({user_id:user}),{status,headers:{'X-TenderSearch-User':user||'anonymous'}});
 const window={location,addEventListener(name,fn){(winEvents[name]??=[]).push(fn);},fetch:async(input)=>{
  const url=typeof input==='string'?input:input.url;state.calls.push(input);
  if(state.offline)throw Error('offline');
  if(url==='/account/identity/'){
   if(defer){defer=false;return new Promise(resolve=>{finish=()=>resolve(response('1'));});}
   return response(state.user);
  }
  if(state.holdAction){const current=state.user;return new Promise(resolve=>{finishAction=()=>resolve(response(current));});}
  return response(state.user,input.headers.get('X-TenderSearch-User')===state.user?200:409);
 }};
 const context={document,window,location,Request,URL,AbortSignal,CustomEvent:class{constructor(type){this.type=type;}}};
 vm.createContext(context);vm.runInContext(code,context);
 return {state,document,window,root,finish:()=>finish(),finishAction:()=>finishAction(),
  event(name){for(const fn of winEvents[name]||[])fn({type:name});},
  submit(form){const event={type:'submit',target:form,preventDefault(){this.prevented=true;},stopImmediatePropagation(){}};document.dispatchEvent(event);return event;}};
}
(async()=>{
 const same=page();await settle();assert.equal(same.document.body.inert,false);
 same.event('blur');assert.equal(same.document.body.inert,true);
 same.event('focus');await settle();assert.equal(same.document.body.inert,false);assert.equal(same.state.cleared,false);
 const switched=page();await settle();switched.event('blur');switched.state.user='2';switched.event('focus');await settle();
 assert.equal(switched.state.cleared,true);assert.equal(switched.state.replaced,'http://localhost:8001/lots/1/preparation/');
 assert.equal(switched.window.TenderSearchAccount.ready,false);
 const logout=page();await settle();logout.event('pagehide');logout.state.user=null;logout.event('pageshow');await settle();
 assert.equal(logout.state.cleared,true);
 const network=page({offline:true});await settle();assert.equal(network.document.body.inert,true);assert.equal(network.root.dataset.accountCheck,'error');
 network.state.offline=false;network.event('focus');await settle();assert.equal(network.document.body.inert,false);
 const race=page({defer:true});race.event('blur');race.state.user='2';race.event('focus');race.finish();await settle();await settle();
 assert.equal(race.state.cleared,true);assert.equal(race.document.body.inert,true);
 const action=page();await settle();action.state.user='2';
 await assert.rejects(action.window.fetch('http://localhost:8001/profiles/1/rename/',{method:'POST',body:'name=private'}));
 assert.equal(action.state.cleared,true);
 const late=page();await settle();late.state.holdAction=true;
 const oldResponse=late.window.fetch('http://localhost:8001/lots/1/preparation/');
 late.event('blur');late.state.user='2';late.event('focus');await settle();
 late.finishAction();await assert.rejects(oldResponse);
 const blocked=page({offline:true});const form={querySelector(){return null;},append(field){this.field=field;}};
 assert.equal(blocked.submit(form).prevented,true);
 await settle();blocked.state.offline=false;blocked.event('focus');await settle();
 assert.equal(blocked.submit(form).prevented,undefined);assert.equal(form.field.value,'1');
 console.log('Private pages: focus/BFCache account change, logout, offline lock, request fencing and stale identity response passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
