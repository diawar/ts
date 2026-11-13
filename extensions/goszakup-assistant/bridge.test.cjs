const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
function bridge(){
 let messageListener,identityListener,user='1',offline=false;
 const sent=[],replies=[],requests=[];
 const location={origin:'http://localhost:8000',pathname:'/lots/1/preparation/'};
 const window={postMessage(data){replies.push(data);},addEventListener(name,fn){if(name==='message')messageListener=fn;}};
 window.top=window;
 const chrome={runtime:{id:'test-extension',onMessage:{addListener(fn){identityListener=fn;}},async sendMessage(message){sent.push(message);return {prepared:true};}}};
 const context={window,location,chrome,document:{readyState:'complete',body:{dataset:{userId:'1'}}},AbortSignal,
  GOSZAKUP_APP_ORIGINS:[location.origin],fetch:async(url,options)=>{requests.push({url,options});if(offline)throw Error('Offline');return {ok:true,json:async()=>({user_id:user})};}};
 vm.createContext(context);vm.runInContext(fs.readFileSync(__dirname+'/bridge.js','utf8'),context);
 return {sent,replies,requests,switchAccount(value){user=value;},offline(){offline=true;},
  prepare:()=>messageListener({source:window,origin:location.origin,data:{channel:'goszakup-preparation-bridge-v1',id:'request_1234567890',extensionId:chrome.runtime.id,type:'prepare',account_id:'forged',packet:{data:{product_name:'Private A'}}}}),
  identity:()=>new Promise(resolve=>identityListener({type:'bridge:identity'},{id:chrome.runtime.id},resolve))};
}
(async()=>{
 const same=bridge();await same.prepare();assert.equal(same.sent.length,1);assert.equal(same.sent[0].account_id,'1');
 assert.equal(same.requests[0].options.credentials,'same-origin');assert.equal(same.requests[0].options.cache,'no-store');
 assert.equal((await same.identity()).origin,'http://localhost:8000');
 const switched=bridge();switched.switchAccount('2');await switched.prepare();
 assert.equal(switched.sent.length,0);assert.match(switched.replies[0].error,/Аккаунт изменился/);
 assert.ok((await switched.identity()).error);
 const unavailable=bridge();unavailable.offline();await unavailable.prepare();assert.equal(unavailable.sent.length,0);
 console.log('Content bridge: live session verification, server-derived identity, stale document refusal and offline failure passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
