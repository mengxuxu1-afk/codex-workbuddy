// SDK simulation: these checks are not evidence of a real WorkBuddy round trip.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(__dirname+'/desktop_session.js','utf8')
  .replace('__PROJECT_ROOT__',JSON.stringify('D:/project'))
  .replace('__DESKTOP_SESSION__',JSON.stringify('original'))
  .replace('__ADAPTER_HASH__',JSON.stringify('test-version'));
const tick = () => new Promise(resolve => setImmediate(resolve));
async function fixture(overrides={}) {
  const events = new Map(), requests = new Map(), calls=[];
  const conversation={
    info:{id:'original',transport:'local',space:{cwd:'D:/project'},state:'idle'},
    configManager:{permissionMode:'fullAccess'},
    getPendingInfo:async()=>false,
    getRequest:async id=>requests.get(id),
    requests:async()=>({items:[...requests.values()].slice(-1)}),
    on:(event,cb)=>{ if(!events.has(event))events.set(event,new Set()); events.get(event).add(cb);return()=>events.get(event).delete(cb); }
  };
  Object.assign(conversation.info,overrides);
  const context=vm.createContext({Date,Map,Number,String,Error,Promise,
    document:{visibilityState:'visible',body:{innerText:'本项目协作事件 7，'}},
    wb:{config:{preference:async()=> 'default-sandbox'},navigation:{route:'/conversation/original'},
      conversations:{get:async id=>{assert.equal(id,'original');return conversation;},currentId:'original',
        prepareInput:async options=>{calls.push(options);}}}});
  await vm.runInContext(source,context);
  return {api:context.__videoEditVisibleBridgeV3,context,conversation,requests,calls,
    emit:async (event,...args)=>{for(const cb of [...(events.get(event)||[])])cb(...args);await tick();}};
}
async function main(){
  const f=await fixture();
  await f.api.start(7,'本项目协作事件 7，任务','video-edit-event-7');
  assert.equal(f.calls.length,1);
  assert.equal(f.calls[0].submitMode,'blocks');
  assert.equal(f.calls[0].target.conversationId,'original');
  await f.api.start(7,'duplicate','video-edit-event-7');assert.equal(f.calls.length,1);
  f.requests.set('other',{id:'other',timestamp:Date.now(),state:'completed',userMessage:{content:[{type:'text',text:'another task'}]}});
  await f.emit('requestUpdate','other');assert.equal((await f.api.read(7)).finished_at,undefined);
  f.requests.set('ours',{id:'ours',timestamp:Date.now(),state:'running',userMessage:{content:[{type:'text',text:'本项目协作事件 7，任务'}]}});
  await f.emit('requestUpdate','ours');
  await f.emit('pendingRequest',{id:'permission-1',type:'command'});
  assert.equal((await f.api.read(7)).permissions[0].approved_by_bridge,false);
  assert.equal((await f.api.cancel(7)).cancel_requested,false);
  f.requests.get('ours').state='error';await f.emit('stateChange');
  assert.equal((await f.api.read(7)).phase,'error');assert.ok((await f.api.read(7)).finished_at);
  assert.equal((await f.api.read(7)).prompt_visible,true);
  const busy=await fixture({state:'running'});
  await assert.rejects(()=>busy.api.start(7,'x','video-edit-event-7'),/desktop_not_ready/);
  assert.equal(busy.calls.length,0);
  const ambiguous=await fixture();await ambiguous.api.start(7,'本项目协作事件 7，','video-edit-event-7');
  for(const id of ['a','b']) {
    ambiguous.requests.set(id,{id,timestamp:Date.now(),state:'running',userMessage:{content:[{type:'text',text:'本项目协作事件 7，'}]}});
    await ambiguous.emit('requestUpdate',id);
  }
  assert.equal((await ambiguous.api.read(7)).phase,'unknown');
  ambiguous.requests.get('a').state='completed';await ambiguous.emit('requestUpdate','a');
  assert.equal((await ambiguous.api.read(7)).phase,'unknown');
  await assert.rejects(()=>vm.runInContext(source.replace('test-version','new-version'),ambiguous.context),/active_adapter/);
  const refreshed=await fixture();assert.equal((await refreshed.api.read(7)).phase,'missing_no_resubmit');
  const missed=await fixture();await missed.api.start(7,'本项目协作事件 7，','video-edit-event-7');
  missed.requests.set('missed',{id:'missed',timestamp:Date.now(),state:'completed',userMessage:{content:[{type:'text',text:'本项目协作事件 7，'}]}});
  assert.equal((await missed.api.read(7)).phase,'returned');
  ambiguous.requests.get('a').state='cancelled';ambiguous.requests.delete('b');
  assert.equal((await ambiguous.api.terminal(7,Date.now()-1000)).matched,true);
  assert.equal(ambiguous.api.active(),false);
  await vm.runInContext(source.replace('test-version','new-version'),ambiguous.context);
  console.log('desktop SDK behavioral assertions passed; no real model call');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
