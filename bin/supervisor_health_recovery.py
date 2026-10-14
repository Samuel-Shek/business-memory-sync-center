"""Sentinel-owned, single-request recovery. No source/business replay or forced stop."""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import hashlib
import fcntl
import os
import stat
import subprocess
import uuid
from datetime import datetime, timedelta
from pathlib import Path
import supervisor_liveness as live

RECEIPT = 'supervisor-health-recovery.json'
ANCHOR = 'supervisor-health-recovery-anchor.json'
LABEL = 'com.example.business-sync-supervisor'
# Existing independent launchd health job (main scheduler remains every 60s).
CHECK_INTERVAL_SECONDS = 300
ROOT = Path(__file__).resolve().parents[1]
PHASES = {'waiting','requested','verifying','verified','blocked','uncertain','failed'}
REASONS = {
    'supervisor_finished_receipt_required':'后台退出后缺少本轮结束凭据，原因待核实',
    'supervisor_owner_failed':'后台本轮检查未正常结束，具体原因待核实',
    'supervisor_exit_nonzero':'后台检查程序异常退出',
    'supervisor_owner_heartbeat_stale':'后台检查进展已超时',
    'supervisor_unloaded':'后台服务未登记，无法安全自动启动',
    'supervisor_owner_receipt_invalid':'后台运行凭据无法验证',
    'supervisor_owner_receipt_missing':'后台运行凭据尚未取得',
    'supervisor_new_owner_pulse_required':'等待故障之后的新运行凭据',
    'supervisor_new_cycle_end_required':'后台已续跑，等待新的本轮结束凭据',
    'supervisor_post_wake_pulse_required':'电脑唤醒后等待新的后台检查',
    'supervisor_deferred_retry_overdue':'原定后台续跑时间已过，尚未取得新进展',
    'supervisor_clock_unavailable':'无法验证系统时间，暂不能确认后台状态',
    'supervisor_service_unknown':'后台服务状态暂不可核实',
    'workers_active':'仍有同步任务运行，等待其结束',
    'owner_active':'后台检查程序仍在运行，继续核验进展',
    'worker_state_unknown':'工作进程退出状态无法验证，暂不请求启动',
    'recovery_record_invalid':'后台恢复记录无法验证，保留原记录',
    'recovery_request_failed':'请求后台恢复未成功，原因待核实',
    'recovery_request_uncertain':'启动请求结果尚不明确，继续核验且不重复请求',
    'maintenance_active':'维护期间暂停后台恢复',
    'recovery_budget_used':'本次故障已请求恢复一次，继续查因和核验',
    'ledger_retry_wait':'原有数据库重试已安排，等待到期续跑',
    'legacy_unrecorded':'历史恢复动作次数未记录，当前原因待核实',
}
LABELS={'waiting':'等待下次检查','requested':'已请求恢复','verifying':'正在核验',
        'verified':'已恢复','blocked':'恢复受阻','uncertain':'正在核验','failed':'恢复受阻'}


def archive_name(alert_id):
    return "supervisor-health-recovery-"+hashlib.sha256(str(alert_id).encode()).hexdigest()+".json"


def clean(d):
    return {k:v for k,v in d.items() if not k.startswith("_") and k!="read_status"}


def archive(root,d):
    if d.get("alert_id"):
        live._write_json(root,archive_name(d["alert_id"]),clean(d))


def read(root, name=RECEIPT):
    d=live._read_json(root,name)
    if d.get('read_status')!='ok':return d
    try:
        uuid.UUID(d['episode_id'])
        if (d.get('schema_version')!=1 or d.get('phase') not in PHASES
            or type(d.get('request_count')) is not int or d['request_count'] not in {0,1}
            or (d.get('request_attempt_count') is not None and (type(d.get('request_attempt_count')) is not int or d['request_attempt_count'] not in {0,1}))
            or (d.get('request_attempt_count') == 1 and d['request_count'] != 1)
            or type(d.get('closed')) is not bool or not live._clock_valid(d['fault_after'])
            or type(d.get('require_cycle_end')) is not bool):raise ValueError()
        for name in ('updated_at','next_check_at'):
            if datetime.fromisoformat(d[name]).tzinfo is None:raise ValueError()
    except (ValueError,KeyError,TypeError):return {'read_status':'invalid'}
    return d


def later(clock, previous):
    return live._clock_valid(clock) and live._clock_valid(previous) and (
        clock['boot_id']!=previous['boot_id'] or clock['active_ns']>previous['active_ns'])


def process_gone(pid):
    if type(pid) is not int or pid<=1:return False
    try:os.kill(pid,0)
    except ProcessLookupError:return True
    except (OSError,ValueError):return False
    return False


def workers_absent(root, snapshot, gone=process_gone):
    # The global controller mutex catches source processes that outlive a main
    # scheduler receipt. Opening never creates/deletes/steals a lock.
    if not live._receipt_valid(snapshot):return False
    for worker in snapshot['workers']:
        if not gone(worker.get('pid')):return False
    try:
        fd=os.open(Path(root)/'state'/'sync-center-control.lock',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        try:
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid():return False
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            fcntl.flock(fd,fcntl.LOCK_UN)
        finally:os.close(fd)
    except (OSError,ValueError):return False
    return True


def request_start(runner=subprocess.run):
    try:
        r=runner(['/bin/launchctl','kickstart',f'gui/{os.getuid()}/{LABEL}'],
                 stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                 stderr=subprocess.DEVNULL,timeout=5,check=False)
        return 'requested' if r.returncode==0 else 'failed'
    except subprocess.TimeoutExpired:return 'uncertain'
    except OSError:return 'failed'


def tick(root, health, service, snapshot, clock, current, *, alert=None,
         allow_request=False, probe=None, runner=subprocess.run,
         gone=process_gone, worker_probe=workers_absent):
    """Caller holds the existing sentinel lock. Reserve before request; never replay."""
    d=read(root)
    anchor=live._read_json(root,ANCHOR)
    if anchor.get('read_status') not in {'ok','missing'} or (anchor.get('read_status')=='ok' and (anchor.get('schema_version')!=1 or anchor.get('initialized') is not True)):
        return {'phase':'blocked','reason':'recovery_record_invalid','closed':False}
    if (d.get('read_status')=='missing' and anchor.get('read_status')=='ok') or (d.get('read_status')=='ok' and anchor.get('read_status')=='missing'):
        return {'phase':'blocked','reason':'recovery_record_invalid','closed':False}
    if d.get('read_status') not in {'ok','missing'}:return {'phase':'blocked','reason':'recovery_record_invalid','closed':False}
    if not live._clock_valid(clock):return {'phase':'blocked','reason':'supervisor_clock_unavailable','closed':False}
    fault=health.get('status') in {'failed','stale','unknown'}
    if d.get('read_status')=='missing' or d.get('closed'):
        if not fault:return d
        if d.get("closed"):archive(root,d)
        if anchor.get("read_status")=="missing":
            live._write_json(root,ANCHOR,dict(schema_version=1,initialized=True,created_at=current.isoformat(timespec="seconds")))
        d=dict(schema_version=1,episode_id=str(uuid.uuid4()),closed=False,
               fault_after=clock,phase='waiting',request_count=0,
               request_attempt_count=0,require_cycle_end=health.get('error_code')=='supervisor_finished_receipt_required',
               alert_id=(alert or {}).get('alert_id'),reason=health.get('error_code'))
    if health.get('error_code')=='supervisor_finished_receipt_required':d['require_cycle_end']=True
    heartbeat=snapshot.get('heartbeat')
    terminal=(snapshot.get('stage') in {'cycle_finished','cycle_deferred'} and
              (snapshot.get('cycle_ended') or snapshot.get('cycle_finished'))==heartbeat)
    if (health.get('can_resolve') is True and later(heartbeat,d['fault_after'])
            and (not d['require_cycle_end'] or terminal)):
        d.update(phase='verified',closed=True,reason=None,verified_at=current.isoformat(timespec='seconds'))
    elif health.get('owner_active') or service.get('state')=='running':
        d.update(phase='verifying',reason='owner_active')
    elif health.get('status') in {'wake_pending','recovery_pending','migration_pending'}:
        d.update(phase='waiting',reason=health.get('error_code'))
    elif not fault:
        d.update(phase='verifying',reason='supervisor_new_cycle_end_required')
    elif d['request_count']:
        # An ambiguous result remains ambiguous until new owner evidence proves recovery.
        if d.get('request_result')=='uncertain':d.update(phase='uncertain',reason='recovery_request_uncertain')
        else:d.update(phase='blocked',reason='recovery_budget_used')
    elif service.get('loaded') is not True or service.get('state') not in {'not running','exited','stopped'}:
        d.update(phase='blocked',reason='supervisor_unloaded' if service.get('loaded') is False else 'supervisor_service_unknown')
    elif not gone(snapshot.get('pid')) or not gone(snapshot.get('ppid')) or not worker_probe(root,snapshot,gone):
        d.update(phase='blocked',reason='worker_state_unknown')
    elif not allow_request:
        d.update(phase='waiting',reason=health.get('error_code'))
    else:
        # Re-probe immediately before reserving. A racing scheduled launch is
        # harmless: kickstart lacks -k and cannot kill or duplicate the singleton.
        fresh=probe() if probe else {}
        if fresh.get('loaded') is not True or fresh.get('state') not in {'not running','exited','stopped'} or not worker_probe(root,snapshot,gone):
            d.update(phase='waiting',reason='workers_active')
        else:
            d.update(phase='uncertain',request_result='uncertain',request_count=1,request_attempt_count=None,
                     reason='recovery_request_uncertain',requested_at=current.isoformat(timespec='seconds'))
            d.update(updated_at=current.isoformat(timespec='seconds'),next_check_at=(current+timedelta(seconds=CHECK_INTERVAL_SECONDS)).isoformat(timespec='seconds'))
            live._write_json(root,RECEIPT,clean(d))
            result=request_start(runner)
            d.update(phase=result,request_result=result,request_attempt_count=1,reason={'requested':health.get('error_code'),'failed':'recovery_request_failed','uncertain':'recovery_request_uncertain'}[result])
    d.update(updated_at=current.isoformat(timespec='seconds'),next_check_at=(current+timedelta(seconds=CHECK_INTERVAL_SECONDS)).isoformat(timespec='seconds'))
    live._write_json(root,RECEIPT,clean(d))
    if d.get("closed"):archive(root,d)
    return d


def view(alert, root=ROOT):
    """Content-free pure projection shared by notifications and workbench."""
    if alert.get('source_id')!='system':return None
    target=alert.get('alert_id')
    if alert.get('category')=='recovery' and str(alert.get('dedupe_key') or '').startswith('system:supervisor_health:recovery:'):
        target=alert['dedupe_key'].split(':')[-1]
    elif alert.get('category')!='supervisor_health':return None
    d=read(root)
    if d.get('read_status')=='ok' and d.get('alert_id')!=target or d.get('read_status')=='missing':
        historic=read(root,archive_name(target))
        if historic.get('read_status')=='ok':d=historic
    matches=d.get('read_status')=='ok' and d.get('alert_id')==target
    resolved=alert.get('status')=='resolved'
    phase='verified' if resolved else d.get('phase','blocked') if matches else 'blocked'
    reason=alert.get('error_code')
    if matches:reason=d.get('reason') or reason
    if not reason:reason='legacy_unrecorded'
    count=d.get('request_attempt_count') if matches else None
    label=LABELS.get(phase,'恢复受阻')
    detail='后台恢复运行；来源处理结果另行核验' if resolved else REASONS.get(reason,'当前原因待核实，保留异常并继续核验')
    attempts=f'主动恢复请求 {count} 次' if count is not None else ('启动请求是否执行尚未确认；本周期预算已占用' if matches and d.get('request_count') else '历史恢复动作次数未记录')
    next_check=d.get('next_check_at') if matches and not resolved else None
    text=f'{label}；{attempts}。{detail}。'
    if next_check:text+='预计下次后台健康检查：'+next_check+'。'
    return dict(phase=phase,label=label,reason=reason,reason_label=detail,
                request_count=count,request_budget_used=bool(matches and d.get('request_count')),next_check_at=next_check,summary=text,
                requires_user=any(x in str(alert.get('error_code') or '') for x in ('permission','auth','login')))
