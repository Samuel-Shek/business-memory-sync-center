(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  let offset = 0;
  const modes = {off:'复用已关闭',observe:'仅观察',exact_reuse:'精确复用已启用'};
  const sourceNames={win11_watch:'电脑业务文件',registered_local:'已登记本地资料',dingtalk_audit_archive:'钉钉测试档案',email_attachment:'邮件附件',dingtalk_chat:'钉钉聊天',dingtalk_minutes:'钉钉听记'};
  const relationNames = {bytes_equal:'字节一致',candidate:'指纹一致候选',scope_equal:'指定范围一致',next_version:'同来源后续版本',derived:'派生',branch:'分叉'};
  function line(parent, tag, text) { const el=document.createElement(tag); el.textContent=text; parent.append(el); return el; }
  async function detail(id) {
    const panel=$('content-relations-detail');panel.replaceChildren();
    try {
      const res=await fetch('/api/content-relations/'+encodeURIComponent(id));if(!res.ok)throw Error();
      const v=(await res.json()).items[0];
      line(panel,'h3',v.metadata.name || '资料详情');
      line(panel,'p','来源：'+(sourceNames[v.source.system]||'已登记来源')+'；观察序号：'+v.ordinal+'；'+(v.lane==='backfill'?'历史登记':'来源事件'));
      line(panel,'p','位置：'+(v.metadata.location || v.metadata.path || '见原始台账'));
      const last=v.receipts.filter(x=>x.action==='extraction_result').at(-1)?.payload;
      const scope=last?.coverage?.scope || v.metadata.coverage || '见本次回执';
      line(panel,'p','读取范围：'+(scope.startsWith('full machine')?'全部机器文字提取与可用 OCR；图片含义和业务事实仍需另行核验':scope));
      line(panel,'p','本次读取：'+(last?.cache?.hit?'已复用通过校验的解析结果':last?'已执行原读取流程':'仅登记历史资料，尚无可用的当前解析回执'));
      line(panel,'p','关系：'+(v.relations.map(x=>relationNames[x.kind] || x.kind).join('、') || '尚无已确认关系'));
      line(panel,'p','副本数量不增加独立证据权重；文件可读不代表已发送或已批准。');
      const details=document.createElement('details');line(details,'summary','查看来源与读取回执');line(details,'pre',JSON.stringify({metadata:v.metadata,receipts:v.receipts,relations:v.relations},null,2));panel.append(details);
    } catch { line(panel,'p','该资料暂不可访问。'); }
  }
  async function load() {
    try {
      const res=await fetch('/api/content-relations?limit=30&offset='+offset);if(!res.ok)throw Error();const v=await res.json();
      $('content-relations-status').textContent=(modes[v.mode]||'未启用')+' · '+v.total+' 次来源登记 · '+(v.relations||0)+' 条可见关系'+' · '+(v.reuse_hits||0)+' 次读取复用'+(v.reason?' · '+v.reason:'');
      const list=$('content-relations-list');list.replaceChildren();
      for(const item of v.items){
        const row=document.createElement('div');row.className='issue-row';
        const button=line(row,'button',item.metadata.name || '资料');button.type='button';button.addEventListener('click',()=>detail(item.id));
        line(row,'p',(sourceNames[item.source.system]||'已登记来源')+' · 观察 '+item.ordinal+' · '+(item.relations.map(x=>relationNames[x.kind]||x.kind).join('、')||'待核对对应关系'));list.append(row);
      }
      $('content-relations-more').hidden=offset+v.items.length>=v.total;
    } catch { $('content-relations-status').textContent='资料关联暂不可用，原同步流程继续。'; }
  }
  $('content-relations-more').addEventListener('click',()=>{offset+=30;load();});
  document.querySelector('[data-page="automatic"]').addEventListener('click',()=>{offset=0;load();});
  load();
})();
