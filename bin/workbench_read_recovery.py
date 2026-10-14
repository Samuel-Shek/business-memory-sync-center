"""One native retry of a proven rolled-back projection after a tested reader fix.

Never clears a lock, resets a budget, repairs a business database, or closes an issue.
The existing ledger verifier still requires natural success and full validation.
"""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

from pathlib import Path
import hashlib,json,fcntl
import ledger_recovery as ledger
NAMES=('model_call_ledger.py','workbench_read_recovery.py','operational_workbench.py','ledger_recovery.py')
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def evidence(root,db):
    pending=ledger.read(ledger.path_for(db,'workbench_items'))
    fault=ledger.read(root/'state/sync-supervisor-last-error.json')
    if pending.get('status')!='blocked' or pending.get('last_failure_at')!=fault.get('recorded_at') or not ledger.model_ledger_read_fault(fault):return None
    release=ledger.read(root/'state/sync-autonomy/release.json')
    if release.get('tests_passed') is not True:return None
    binding={n:sha(root/'bin'/n) for n in NAMES}
    if any(release.get('code_binding',{}).get(n)!=h for n,h in binding.items()):return None
    tests=release.get('tests',[])
    if not tests or not any(Path(t.get('path','')).name=='selftest_workbench_read_recovery.log' for t in tests):return None
    if any(sha(Path(t['path']))!=t.get('sha256') for t in tests):return None
    token=hashlib.sha256(json.dumps([fault,binding],sort_keys=True).encode()).hexdigest()
    return {'token':token,'binding':binding,'failure_at':fault['recorded_at'],'prior_attempts':pending.get('attempts',0)}

def retry_projection(root,db,operation):
    if not ledger.native():raise ledger.Deferred('workbench_items')
    try:proof=evidence(root,db)
    except (OSError,ValueError,KeyError):proof=None
    if proof is None:raise ledger.Deferred('workbench_items')
    directory=root/'state/workbench-read-recovery';directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    with ledger.locked(directory/'recovery.lock',blocking=False) as owner:
        if not owner:raise ledger.Deferred('workbench_items')
        receipt=directory/(proof['token']+'.json')
        if receipt.exists():raise ledger.Deferred('workbench_items')
        import model_call_ledger,business_sync_state as states,sqlite_write_transactions
        # Fresh SQL-read-only check before granting this exact non-business projection.
        model_call_ledger.report(root/'state/model-calls',operations_only=True)
        row={**proof,'state':'started','at':ledger.stamp(),'attempt_consumed':True,'budget_reset':False,'business_replayed':False}
        ledger.atomic(receipt,row)
        try:
            result=ledger.run(db,'workbench_items',lambda:sqlite_write_transactions.write_transaction(db,operation,connector=states._connect,phase='workbench_items'),ignore_due=True)
        except BaseException as exc:
            ledger.atomic(receipt,{**row,'state':'not_verified','error_type':type(exc).__name__});raise
        ledger.atomic(receipt,{**row,'state':'projection_reconciled','completed_at':ledger.stamp(),'native':True})
        return result
