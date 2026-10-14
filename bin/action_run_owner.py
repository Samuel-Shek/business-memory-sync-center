"""Task ownership evidence. Never infer business success from process exit."""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import fcntl
import json
import os
import shlex
import signal
import subprocess
import tempfile
import threading
import uuid
from contextlib import contextmanager, ExitStack
from datetime import datetime
from pathlib import Path
import supervisor_liveness as life

TARGETS = frozenset({'dingtalk_chat','dingtalk_minutes','dingtalk_mail','win11','wechat','all','dingtalk_all'})
WORKERS = frozenset({'business_sync_controller.py','dingtalk_sync_orchestrator.py','dingtalk_minutes_memory.py',
 'email_sync.py','email_phase_worker.py','sync_dingtalk_chat.py','win11_memory_automation.py','win11_semantic_merge.rb',
 'business_sync_now.sh','run_wechat_pipeline.sh','collect_wechat_chat.py','process_wechat_media.py','wechat_ai_queue.py'})

def now(): return datetime.now().astimezone().isoformat(timespec='seconds')
def owner_path(root, run_id): return Path(root)/'state/action-run-owners'/(str(uuid.UUID(run_id))+'.json')
def write(path, value):
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    if path.is_symlink() or path.parent.is_symlink(): raise ValueError('owner_path_unsafe')
    fd,tmp=tempfile.mkstemp(prefix='.owner-',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as f:
            json.dump(value,f,sort_keys=True);f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)

@contextmanager
def lease(root, run_id, target):
    pid=os.getpid();identity=life.process_identity(pid);clock=life.clock_sample()
    if not identity or not clock.get('boot_id'):raise RuntimeError('action_owner_identity_unavailable')
    path=owner_path(root,run_id)
    row={'schema_version':1,'run_id':run_id,'target':target,'pid':pid,'process_identity':identity,
         'boot_id':clock['boot_id'],'started_at':now(),'heartbeat_at':now(),'state':'running'}
    write(path,row);stop=threading.Event();errors=[]
    def heartbeat():
        while not stop.wait(30):
            try:write(path,{**row,'heartbeat_at':now()})
            except Exception as exc:errors.append(type(exc).__name__);return
    thread=threading.Thread(target=heartbeat,daemon=True);thread.start()
    try:yield row
    finally:
        stop.set();thread.join(2)
        # Exit is ownership evidence only; action_runs remains the completion authority.
        try:
            write(path,{**row,'state':'executor_exited','heartbeat_at':now(),'exited_at':now(),
                        'heartbeat_error':errors[0] if errors else None})
        except Exception:
            # Never replace a committed business result with an ownership logging error.
            # The retained receipt still has a PID birth identity for later reconciliation.
            pass

def process_state(row):
    try:
        pid=int(row['pid']);current=life.process_identity(pid)
        if current:return 'alive' if current==row['process_identity'] else 'replaced'
        try:os.kill(pid,0)
        except ProcessLookupError:return 'gone'
        except (PermissionError,OSError):return 'unknown'
        return 'unknown'
    except (KeyError,TypeError,ValueError):return 'unknown'

def worker_present():
    result=subprocess.run(['ps','-axo','pid=,command='],capture_output=True,text=True,timeout=5)
    if result.returncode:raise RuntimeError('process_scan_unavailable')
    for line in result.stdout.splitlines():
        parts=line.strip().split(None,1)
        if len(parts)!=2:continue
        try:args=shlex.split(parts[1])
        except ValueError:raise RuntimeError('process_scan_unparseable')
        # Ignore the inspection shell's script string; examine executable/script arguments only.
        if any(Path(a).name in WORKERS for a in args[:3]):return True
        if any(Path(a).name=='dingtalk_memory_automation.py' for a in args[:3]) and 'listener' not in args:return True
    return False

def recover(root):
    """Guarded all-source idle boundary. Unknown liveness does not authorize changes."""
    root=Path(root)
    if not (root/'config.json').is_file():return {'status':'not_installed','recovered':0}
    import dingtalk_sync_v2 as syncv2
    try:
        with ExitStack() as stack:
            # Same ordering as controller: admission, then shared AI. Never unlink locks.
            for path in [root/'state/sync-center-control.lock',root/'state/dingtalk-shared-ai.lock',
                         syncv2.SOURCE_LOCK,syncv2.SEMANTIC_LOCK,root/'state/memory-index.lock']:
                fd=os.open(path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
                h=stack.enter_context(os.fdopen(fd,'a+'));fcntl.flock(h,fcntl.LOCK_EX|fcntl.LOCK_NB)
            if worker_present():return {'status':'workers_active','recovered':0}
            c=syncv2.connect_readonly()
            try:rows=[dict(r) for r in c.execute("SELECT * FROM action_runs WHERE status='running'")]
            finally:c.close()
            recovered=[]
            for row in rows:
                if row['action_type'] not in TARGETS:continue
                try:
                    age=(datetime.now().astimezone()-datetime.fromisoformat(row['updated_at'] or row['started_at'])).total_seconds()
                    if age<120:continue
                    p=owner_path(root,row['run_id'])
                    if p.exists():
                        if p.is_symlink():continue
                        owner=json.loads(p.read_text())
                        if owner.get('run_id')!=row['run_id'] or owner.get('target')!=row['action_type']:continue
                        if owner.get('state') == 'legacy_boundary_verified':
                            if owner.get('original_updated_at') != row['updated_at'] or not owner.get('migration_receipt'):continue
                            state='legacy_idle_boundary'
                        else:
                            state=process_state(owner)
                            if state not in {'gone','replaced'}:continue
                    else:
                        # Unknown legacy ownership needs a bounded migration receipt, never just age.
                        continue
                    # Recheck while admission is fenced; never act on a scan before the locks.
                    if worker_present():break
                    receipt={'reason':'executor_exit_verified','owner_state':state,
                             'checked_at':now(),'business_completion_verified':False,'original_updated_at':row['updated_at']}
                    syncv2.finish_action_run(row['run_id'],'interrupted',result=receipt,error_code='interrupted_after_process_exit', expected_status='running')
                    recovered.append(row['run_id'])
                except (ValueError,KeyError,TypeError,OSError):continue
            return {'status':'checked','recovered':len(recovered),'run_ids':recovered}
    except BlockingIOError:return {'status':'busy','recovered':0}
    except Exception as exc:return {'status':'unavailable','error_code':type(exc).__name__,'recovered':0}
