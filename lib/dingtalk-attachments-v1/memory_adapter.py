"""Deterministic governed source-summary writer. No model-defined paths/actions."""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import json
from pathlib import Path
from handoff_core import canonical,digest,Rejected,validated_summary


def check_commit(journal,key,input_hash):
    data=json.loads(Path(journal).read_text('utf-8'))
    if data.get('state')!='committed' or data.get('batch_id')!='dingtalk-attachment-'+key:
        raise Rejected('memory_commit_unconfirmed')
    changed=[p for p in data['paths'] if p.get('expected_applied_sha256')]
    if len(changed)!=1: raise Rejected('memory_commit_unconfirmed')
    for entry in changed:
        path=Path(entry['target'])
        if path.is_symlink() or digest(path.read_bytes())!=entry['expected_applied_sha256']:
            raise Rejected('memory_committed_file_changed')
    return dict(task_key=key,input_sha256=input_hash,transaction_id=data['batch_id'],
                committed=True,validation_passed=True)


def apply(config,ledger,key,writer):
    row=ledger.get(key)
    if row['stage']=='memory_applied': return {'writes':0,'no_op':True}
    if row['stage']!='m2_received': raise Rejected('source_not_received')
    summary=json.loads(row['summary']); parsed=json.loads(row['receipt'])
    validated_summary(summary,parsed['text'],'gpt-6-astra')
    input_hash=digest(row['summary'].encode())
    transaction='dingtalk-attachment-'+key
    journal=writer.runtime_paths(config)['runs']/transaction/'apply_journal.json'
    lock=writer.acquire_lock(writer.MEMORY_LOCK)
    generation=None
    try:
        generation=ledger.claim(key)
        if generation is None: raise Rejected('worker_busy_or_uncertain')
        if journal.exists():
            receipt=check_commit(journal,key,input_hash)
            # After crash, reverify actual current library before adopting.
            writer.run_refresh_and_validate(config,lock_held=True)
            writes=0
        else:
            writer.run_refresh_and_validate(config,lock_held=True,refresh_indexes=False)
            memory_root=Path(config['memory_root'])
            records=writer.memory_records(memory_root)
            now=writer.now_iso(); date=now[:10].replace('-','')
            memory_id=writer.next_memory_id(records,date,set(),vault_root=memory_root.parent)
            path=memory_root/'04_对话来源摘要'/('钉钉附件来源摘要-'+key[:16]+'.md')
            if path.exists(): raise Rejected('memory_target_collision')
            lines=[]
            for item in summary['items']:
                text=writer.sanitize_note_text(item['summary'],2000)
                lines.append('- 材料摘要：'+text+'（全文字符 '+str(item['start'])+'–'+str(item['end'])+'）')
            body='## 来源摘要\n\n'+('\n'.join(lines) or '本材料暂无可提炼的业务摘要。')+'\n\n## 核验说明\n\n以上仅为附件内容提炼，未独立核实，不认证客户事实、发送、批准或项目进展。项目关联未核实时不自动建立。\n\n## 来源\n\n`dingtalk-attachment:'+key+'`；全文与逐项原文引用保存在私有来源索引，原件保留在 Win11。\n'
            fm=[('memory_id',memory_id),('category','conversation_summary'),('memory_scope','conversation_source'),
                ('memory_kind','conversation_summary'),('origin_type','internal_communication'),('status','active'),
                ('evidence_level','internal_source'),('sensitivity','confidential'),('source_system','dingtalk'),
                ('source_check','matched'),('fact_status','not_applicable'),('source_ref','dingtalk-attachment:'+key),
                ('observed_at',now),('processed_at',now),('reviewed_at',None),('valid_until',None),('supersedes',[]),
                ('content_hash',''),('applies_to',['project:business-energy'])]
            result=writer.apply_changes(config,transaction,{path:writer.render_note(fm,body)},lock_held=True)
            if result.get('validated') is not True: raise Rejected('memory_validation_failed')
            receipt=check_commit(journal,key,input_hash); writes=result['writes']
        ledger.memory_applied(key,generation,receipt)
        return {'writes':writes,'no_op':not bool(writes)}
    except Exception:
        if generation is not None and ledger.get(key)['status']=='running':
            # Release only the receive->writer lease. Retain journal, do not
            # pretend rollback/commit succeeded or replay an unknown journal.
            ledger.await_writer(key,generation)
        raise
    finally: lock.close()
