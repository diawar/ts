const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
async function run({failure='',switchAccount=false}={}){
 let handler,identities=0;const created=[],closed=[],executed=[];
 const chrome={runtime:{id:'extension',onMessage:{addListener:fn=>handler=fn},getURL:p=>'chrome-extension://extension/'+p},
  alarms:{onAlarm:{addListener(){}},clear:async()=>{}},storage:{session:{get:async()=>({})}},
  tabs:{sendMessage:async()=>({origin:'http://localhost:8000',pathname:'/workspace/drafts/',user_id:switchAccount&&++identities>1?'2':'1'}),
   create:async options=>{created.push(options);return {id:77};},get:async()=>{if(failure==='load')throw Error('Load failed');return {status:'complete'};},
   remove:async id=>closed.push(id),onUpdated:{addListener(){},removeListener(){}}},
  scripting:{executeScript:async options=>{executed.push(options);if(failure==='script')throw Error('Script failed');return [{result:{check_state:'found'}}];}}};
 const context={chrome,URL,setTimeout,clearTimeout,importScripts(){},GOSZAKUP_APP_ORIGINS:['http://localhost:8000'],checkSavedApplication(){}};
 vm.runInNewContext(fs.readFileSync('extensions/goszakup-assistant/background.js','utf8'),context);
 const reply=await new Promise(resolve=>{
  const accepted=handler({type:'bridge:queue-check',account_id:'1',target:{company_bin:'241040001415',announcement_number:'17920228-1',lot_number:'88537093-ЗЦП1',number:'123'}},
   {id:'extension',frameId:0,tab:{id:1},url:'http://localhost:8000/workspace/drafts/'},resolve);
  assert.equal(accepted,true);
 });
 assert.equal(created.length,1);assert.equal(created[0].active,false);assert.deepEqual(closed,[77]);
 assert.equal(executed.length,failure==='load'?0:1);
 if(failure||switchAccount)assert.ok(reply.error);else assert.equal(reply.result.check_state,'found');
}
(async()=>{for(const options of [{},{failure:'load'},{failure:'script'},{switchAccount:true}])await run(options);
 console.log('Extension background checks: inactive tabs, cleanup on success/failure and account-change fencing passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
