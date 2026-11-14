const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('extensions/goszakup-assistant/bridge.js','utf8');
async function verify({allowed=true,owner='1',active=false}={}){
 let handler;const requests=[];
 const window={addEventListener(){}};window.top=window;
 const context={window,document:{readyState:'complete',body:{dataset:{userId:'1'}},querySelector:()=>({value:'csrf'})},location:{origin:'http://localhost:8000',pathname:'/workspace/drafts/'},
  GOSZAKUP_APP_ORIGINS:['http://localhost:8000'],URL,JSON,AbortSignal,
  chrome:{runtime:{id:'extension',onMessage:{addListener:fn=>handler=fn}}},
  fetch:async(url,options)=>{requests.push({url,options});return {ok:true,json:async()=>url==='/account/identity/'?{user_id:owner}:{allowed}};}};
 vm.runInNewContext(source,context);
 const response=await new Promise(resolve=>handler({type:'bridge:identity',queue_context:{id:12,generation:'11111111-1111-4111-8111-111111111111'},queue_active:active},{id:'extension'},resolve));
 if(owner!=='1'){assert.ok(response.error);assert.equal(requests.length,1);return;}
 assert.equal(requests[1].url,'/workspace/drafts/12/guard/');assert.equal(requests[1].options.method,'POST');
 assert.equal(requests[1].options.headers['X-CSRFToken'],'csrf');assert.equal(requests[1].options.headers['X-TenderSearch-User'],'1');
 assert.equal(JSON.parse(requests[1].options.body).active,active);
 if(allowed)assert.equal(response.queue_allowed,true);else assert.ok(response.error);
}
(async()=>{await verify();await verify({active:true});await verify({allowed:false});await verify({owner:'2'});
 console.log('Extension queue guard: owner, CSRF, run generation and active action reservation passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
