const {test}=require('node:test');const assert=require('node:assert/strict');const fs=require('node:fs');const vm=require('node:vm');const path=require('node:path');
const code=fs.readFileSync(path.join(__dirname,'../decision-center/app.js'),'utf8');
const fragment=code.slice(code.indexOf('function latestManualAll()'),code.indexOf('function normalizeAiProfile('));
function pick(core){const ctx=vm.createContext({ui:{core}});vm.runInContext(fragment+';globalThis.result=latestSyncAction();',ctx);return ctx.result;}
test('a completed automatic source stays visible after its running card ends',()=>{
 const old={run_id:'old',action_type:'all',completed_at:'2020-01-02T10:55:00+08:00'};
 const latest={run_id:'new',action_type:'dingtalk_minutes',completed_at:'2020-01-02T22:26:00+08:00',status:'deferred',error_code:'waiting_power'};
 assert.equal(pick({latest_manual_actions:{all:old},latest_actions:{all:old,dingtalk_minutes:latest}}).run_id,'new');
});
test('a newer diagnostic or conversation does not replace sync outcome',()=>{
 const sync={run_id:'sync',action_type:'dingtalk_mail',completed_at:'2020-01-02T22:00:00+08:00'};
 assert.equal(pick({latest_actions:{dingtalk_mail:sync,diagnostic:{run_id:'diag',action_type:'alert_conversation',completed_at:'2020-01-02T23:00:00+08:00'}}}).run_id,'sync');
});
test('older clients retain manual fallback without a latest actions map',()=>assert.equal(pick({latest_manual_actions:{all:{run_id:'fallback',action_type:'all'}}}).run_id,'fallback'));
