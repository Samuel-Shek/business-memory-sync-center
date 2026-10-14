'use strict';
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict'),{test}=require('node:test');
const esc=x=>String(x??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
const context=vm.createContext({ui:{},api:()=>{},esc,formatTime:x=>x||'待核验'});
const code=fs.readFileSync(path.join(__dirname,'../decision-center/workbench.js'),'utf8').replace('return { listPath,','return { listenerAcceptanceSummary, listPath,');
vm.runInContext(code+';globalThis.w=workbenchWorkspace;',context);
function fixture(){return {observation:{original_failure:'旧故障',last_success:'历史来源成功',listener_acceptance:{status:'pending',ready_at:'本轮启动时间',checked_at:'验收检查时间',evidence_current_episode:true,subscription_verification:'pending',historical_accepted_at:'旧验收时间',scopes:{direct:{accepted:false,received_events:0},group:{accepted:false,received_events:2,older_message_events:2}}}}};}
test('pending old events remain separate from accepted incoming messages',()=>{const html=context.w.listenerAcceptanceSummary(fixture());assert.match(html,/监听运行中/);assert.match(html,/私聊：等待/);assert.match(html,/2 条对应启动前消息/);assert.match(html,/不代表本轮通过/);assert.match(html,/订阅验证：等待有效回执/);});
test('single scope and unavailable counts are stated independently',()=>{const v=fixture();v.observation.listener_acceptance.scopes.group={accepted:true,received_events:null};const html=context.w.listenerAcceptanceSummary(v);assert.match(html,/群聊：新消息验收已通过/);assert.match(html,/私聊：等待/);assert.match(html,/计数暂不可核验/);});
test('full pass and raw history escape',()=>{const v=fixture();v.observation.original_failure='<img src=x onerror=1>';v.observation.listener_acceptance.status='passed';v.observation.listener_acceptance.subscription_verification='live_delivery';const html=context.w.listenerAcceptanceSummary(v);assert.match(html,/本轮监听验收已通过/);assert.match(html,/已由真实投递确认/);assert.doesNotMatch(html,/<img/);});
test('legacy details render without the optional field',()=>assert.equal(context.w.listenerAcceptanceSummary({observation:{}}),''));
