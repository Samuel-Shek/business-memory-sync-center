"""Content-free index failure evidence; historical events are never overwritten."""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import hashlib,json,os,re,tempfile,uuid
from datetime import datetime
from pathlib import Path
CODES=frozenset({'memory_index_state_unavailable','memory_index_state_invalid','memory_index_update_failed'})
PHASES=frozenset({'index_refresh','index_status_repair','index_status_commit','index_state'})
TYPES=frozenset({'IndexRefreshFault','ValidationFault','OperationalError','DatabaseError','IntegrityError','OSError','PermissionError','FileNotFoundError','TimeoutExpired','CalledProcessError','RuntimeError','ValueError','KeyError','TypeError'})
def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()
def safe_exception(exc):
    result={'error_type':type(exc).__name__ if type(exc).__name__ in TYPES else 'OtherError'}
    code=getattr(exc,'code',None)
    if code in CODES:result['fault_code']=code
    if type(getattr(exc,'returncode',None)) is int:result['returncode']=exc.returncode
    if getattr(exc,'subprocess_error_type',None) in {'NoMethodError','SyntaxError','LoadError','PermissionError','JSON::ParserError'}:result['subprocess_error_type']=exc.subprocess_error_type
    cause=exc.__cause__
    if cause is not None:
        result['cause_type']=type(cause).__name__ if type(cause).__name__ in TYPES else 'OtherError'
        number=getattr(cause,'sqlite_errorcode',None)
        if type(number) is int:result['sqlite_code']=number
    return result

def atomic(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd,tmp=tempfile.mkstemp(prefix='.index-evidence-',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as f:json.dump(value,f,sort_keys=True);f.write('\n');f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
        fd=os.open(path.parent,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)

def record_failure(root,phase,generation,exc):
    if phase not in PHASES:raise ValueError('invalid_index_phase')
    event={'schema_version':1,'event_id':uuid.uuid4().hex,'recorded_at':datetime.now().astimezone().isoformat(timespec='microseconds'),
           'phase':phase,'index_generation':generation if type(generation) is int and generation>=0 else None,**safe_exception(exc)}
    event['evidence_sha256']=digest(event)
    atomic(Path(root)/'state/index-failures'/(event['event_id']+'.json'),event)
    return event

from contextlib import contextmanager
import fcntl
@contextmanager
def runtime_event_guard(root):
    path=Path(root)/'state/index-recovery.lock';path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    with path.open('a+') as f:
        fcntl.flock(f,fcntl.LOCK_EX)
        try:yield
        finally:fcntl.flock(f,fcntl.LOCK_UN)

class IndexRefreshFault(RuntimeError):
    def __init__(self,returncode,output):
        super().__init__('index_refresher_failed')
        self.returncode=returncode
        self.subprocess_error_type=next((kind for kind in ('NoMethodError','SyntaxError','LoadError','PermissionError','JSON::ParserError') if re.search(r'\b'+re.escape(kind)+r'\b',output)),None)
