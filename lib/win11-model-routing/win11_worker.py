"""One bounded job on the existing SSH channel, never a timer or daemon."""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from handoff_core import canonical,digest
from win11_download_probe import ROOT,acquire
from win11_parse_canary import atomic_new

FILES=('parse-result.json','full-text.txt','summary-result.json','summary-receipt.json')


def model_calls(job):
    receipts=list((job/'route-v2').glob('round-*/route-receipt.json'))
    if receipts:
        return sum(a.get('category')!='cooling_down' for p in receipts for a in json.loads(p.read_text('utf-8')).get('attempts',[]))
    return int((job/'model-started.json').exists())


def safe_code(exc):
    value=str(exc)
    return value if re.fullmatch('[a-zA-Z0-9_]{1,100}',value) else type(exc).__name__


def job_run(request):
    if os.name!='nt': raise ValueError('windows_required')
    key=request.pop('request_key')
    action=request.pop('action','run')
    if action not in ('run','status','acquire'): raise ValueError('action_invalid')
    if not re.fullmatch('[a-f0-9]{64}',key): raise ValueError('request_invalid')
    # Key covers ALL request fields, not an ID supplied by the model.
    if digest(canonical(request).encode())!=key: raise ValueError('request_binding_mismatch')
    ROOT.mkdir(parents=True,exist_ok=True)
    job=ROOT/key[:32]
    if not job.exists():
        if action=='status': return {'status':'not_received'}
        job.mkdir()
        who=subprocess.run(['whoami','/user','/fo','csv','/nh'],capture_output=True,text=True,check=True).stdout
        sid=re.search(r'S-1-5-[0-9-]+',who)
        if not sid: raise ValueError('identity_missing')
        subprocess.run(['icacls',str(job),'/inheritance:r','/grant:r','*'+sid[0]+':(OI)(CI)F','*S-1-5-18:(OI)(CI)F'],capture_output=True,check=True)
        atomic_new(job/'request.json',canonical(request).encode())
    if job.is_symlink() or json.loads((job/'request.json').read_text('utf-8'))!=request:
        raise ValueError('request_binding_mismatch')
    import msvcrt
    with (job/'worker.lock').open('a+b') as lock:
        lock.seek(0)
        try: msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        except OSError: return {'status':'running'}
        try:
            if all((job/f).is_file() for f in FILES):
                return {'status':'summarized','model_calls':0,'files':{f:digest((job/f).read_bytes()) for f in FILES}}
            if action=='status':
                if (job/'worker-error.json').exists(): return json.loads((job/'worker-error.json').read_text('utf-8'))
                return {'status':'uncertain'}
            started_before=model_calls(job)
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    acquire(job,request)
                    if action=='acquire': return {'status':'win11_received','model_calls':0}
                    from win11_parse_canary import main as parse
                    asyncio.run(parse(key[:32]))
                    if not (job/'parse-result.json').exists(): raise ValueError('partial_content')
                    from win11_summary_canary import main as summarize
                    reconcile=(job/'model-started.json').exists()
                    summarize(key[:32],reconcile=reconcile)
                return {'status':'summarized','model_calls':max(0,model_calls(job)-started_before),
                    'files':{f:digest((job/f).read_bytes()) for f in FILES}}
            except Exception as exc:
                result={'status':'blocked','code':safe_code(exc),
                    'model_calls':max(0,model_calls(job)-started_before)}
                # Preserve prior failures rather than erase history on retry.
                if not (job/'worker-error.json').exists(): atomic_new(job/'worker-error.json',canonical(result).encode())
                return result
        finally:
            lock.seek(0); msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1)


if __name__=='__main__':
    try:
        payload=sys.stdin.buffer.readline(1024*1024)
        print(canonical(job_run(json.loads(payload))))
    except Exception as exc:
        print(canonical({'status':'blocked','code':safe_code(exc)}))
        sys.exit(1)
