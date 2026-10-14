'use strict';
const $=s=>document.querySelector(s);
const levelNames={act:'需要你动手',watch:'后台处理中 / 提醒',info:'正常'};
const sectionEmpty={sync:'目前没有未关闭的总告警。',chat_alert:'群聊库里目前没有未处理的告警。',chat_failed:'目前没有失败的聊天整理项。',listener:'读不到实时监听状态。'};
function el(tag,className,text){const node=document.createElement(tag);if(className)node.className=className;if(text!==undefined)node.textContent=text;return node;}
function line(label,text){const p=el('p','line');p.append(el('strong','',label+'：'),document.createTextNode(text));return p;}
function card(item){
  const article=el('article','card level-'+item.level);
  const head=el('div','card-head');head.append(el('span','badge',levelNames[item.level]||item.level),el('h3','',item.title));article.append(head);
  article.append(line('出了什么事',item.what),line('影响什么',item.impact),line('要不要你动手',item.action));
  if(item.facts.length){const dl=el('dl','facts');for(const [k,v] of item.facts){dl.append(el('dt','',k),el('dd','',v));}article.append(dl);}
  if(item.rows.length){const wrap=el('div','table-wrap');const table=el('table');const thead=el('thead');const tr=el('tr');for(const h of ['会话','消息时间','类型'])tr.append(el('th','',h));thead.append(tr);const tbody=el('tbody');for(const row of item.rows){const r=el('tr');for(const v of row)r.append(el('td','',v));tbody.append(r);}table.append(thead,tbody);wrap.append(table);article.append(wrap);}
  return article;
}
let requestSequence=0;
async function refresh(){
  const sequence=++requestSequence;$('#status').textContent='正在读取…';$('#refresh').disabled=true;
  try{
    const response=await fetch('/api/alerts-overview',{cache:'no-store'});
    if(!response.ok)throw Error('读取失败，请返回同步中心刷新后重试。');
    const data=await response.json();if(sequence!==requestSequence)return;
    for(const section of document.querySelectorAll('section[data-section]')){
      const box=section.querySelector('.cards');box.replaceChildren();
      const order={act:0,watch:1,info:2};
      const items=data.items.filter(item=>item.section===section.dataset.section).sort((a,b)=>(order[a.level]??3)-(order[b.level]??3));
      if(items.length)for(const item of items)box.append(card(item));else box.append(el('p','empty',sectionEmpty[section.dataset.section]));
    }
    $('#status').textContent=`数据读取于 ${data.generated_label}`;
    let summary=data.act_count?`现在有 ${data.act_count} 项需要你动手（标红的卡片），${data.watch_count} 项后台在处理或只是提醒。`:`现在没有需要你动手的项目；${data.watch_count} 项后台在处理或只是提醒。`;
    if(data.unsent_count)summary+=`另外，总告警里有 ${data.unsent_count} 条超过 1 小时还没推送出去，你可能没收到通知。`;
    $('#summary').textContent=summary;
  }catch(e){if(sequence===requestSequence)$('#status').textContent=e.message;}
  finally{if(sequence===requestSequence)$('#refresh').disabled=false;}
}
$('#refresh').addEventListener('click',refresh);refresh();
