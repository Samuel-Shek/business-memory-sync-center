#!/usr/bin/env python3
"""Bounded local recovery only: no model, provider, account or arbitrary command."""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import argparse
import json
import os
import sqlite3
import subprocess
import threading
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Callable
import dingtalk_memory_automation as memory
import dingtalk_sync_v2 as syncv2
import business_sync_controller as controller
import business_sync_state as state

ROOT = Path(__file__).resolve().parents[1]
DISPATCH_LOCK = ROOT / 'state/local-repair-dispatch.lock'


def wake_supervisor() -> bool:
    """Wake only the existing owner; no caller-supplied command or job label."""
    try:
        result = subprocess.run(['/bin/launchctl', 'kickstart',
                                 f'gui/{os.getuid()}/com.example.business-sync-supervisor'],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=5, check=False)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        # The durable request remains queued for the regular 60-second tick.
        return False


def owner_alive(pid: Any) -> bool:
    if not pid:
        return False
    try:
        value = int(pid)
        if value <= 1:
            raise ValueError('invalid_repair_owner')
        os.kill(value, 0)
        return True
    except ProcessLookupError:
        return False


@contextmanager
def supervisor_heartbeat():
    """Keep the existing supervisor healthy during bounded local validation."""
    stopped = threading.Event()
    def beat() -> None:
        while not stopped.wait(10):
            try:
                state.update_runtime(heartbeat_at=state.now_iso())
            except (OSError, sqlite3.Error):
                pass
    worker = threading.Thread(target=beat, daemon=True, name='local-repair-heartbeat')
    worker.start()
    try:
        yield
    finally:
        stopped.set()
        worker.join(timeout=1)


def maintenance_active() -> bool:
    try:
        return json.loads((ROOT / 'state/maintenance-mode.json').read_text()).get('status') != 'inactive'
    except (OSError, ValueError):
        return True


def eligible(alert: dict[str, Any]) -> bool:
    if alert.get('status') != 'open' or alert.get('category') == 'recovery':
        return False
    code = str(alert.get('error_code') or '')
    if code in {'validation_inputs_changed', 'automatic_validation_timeout', 'automatic_validation_interrupted', 'automatic_validation_state_invalid'}:
        # The independent validator owns its durable three-attempt budget.
        # A second repair path must not silently add another three attempts.
        return False
    if any(token in code.lower() for token in ('permission', 'auth', 'login')):
        return False
    if alert.get('source_id') == 'memory_core':
        return alert.get('category') == 'core_failure'
    return alert.get('source_id') == 'dingtalk_chat' and any(
        token in code for token in ('rollback_', 'validation_', 'recovery_conflict', 'memory_index_refresh')
    )


def require_index_ready() -> None:
    """Do not take the receiver's write lock away while its index is pending."""
    path = ROOT / 'state/bridge.sqlite'
    try:
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1) as connection:
            values = dict(connection.execute("SELECT key,value FROM maintenance_state WHERE key IN "
                                             "('index_dirty_generation','index_applied_generation','index_last_error')"))
        dirty = int(values['index_dirty_generation'])
        applied = int(values['index_applied_generation'])
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError) as exc:
        raise memory.AutomationError('memory_index_state_unavailable', '无法核验索引更新进度') from exc
    if applied < 0 or dirty < applied:
        raise memory.AutomationError('memory_index_state_invalid', '索引代次异常，停止自动修复')
    if dirty > applied:
        if values.get('index_last_error'):
            raise memory.AutomationError('memory_index_update_failed', '索引更新已有明确错误，停止自动修复')
        raise memory.AutomationError('memory_index_update_pending', '等待接收器提交索引后再校验；尚未执行修复')


def automatic_budget_available(alert: dict[str, Any]) -> bool:
    return alert.get('repair_status') == 'not_attempted' and int(alert.get('repair_attempt_count') or 0) == 0


def failure_repair_status(deferred: bool, attempt_count: int) -> str:
    # A deferred manual request must not re-arm an already consumed auto budget.
    return 'not_attempted' if deferred and attempt_count == 0 else 'blocked'


def _update(alert_id: str, **fields: Any) -> None:
    allowed = {'repair_status', 'repair_attempt_count', 'repair_started_at', 'repair_finished_at', 'repair_error_code', 'repair_pid', 'repair_run_id'}
    if set(fields) - allowed:
        raise ValueError('repair_state_field_invalid')
    with state._connect() as connection:
        connection.execute('UPDATE sync_alerts SET ' + ','.join(name + '=?' for name in fields) + ',updated_at=? WHERE alert_id=?',
                           (*fields.values(), state.now_iso(), alert_id))


def start(config_path: Path, alert_id: str, *, automatic: bool = False, requested_run_id: str | None = None) -> dict[str, Any]:
    uuid.UUID(alert_id)
    if maintenance_active():
        return {'status': 'deferred', 'error_code': 'maintenance_active'}
    state.initialize()
    with syncv2.file_lock(DISPATCH_LOCK, blocking=False):
        with state._connect(readonly=True) as connection:
            found = connection.execute('SELECT * FROM sync_alerts WHERE alert_id=?', (alert_id,)).fetchone()
        alert = dict(found) if found else {}
        if not eligible(alert):
            return {'status': 'blocked', 'error_code': 'repair_not_allowlisted'}
        if alert.get('repair_status') in {'queued', 'running'}:
            pid = alert.get('repair_pid')
            if alert.get('repair_status') == 'queued' and pid is None:
                return {'status': 'already_running', 'run_id': alert.get('repair_run_id'), 'queued': True}
            try:
                if owner_alive(pid):
                    return {'status': 'already_running', 'run_id': alert.get('repair_run_id')}
            except (PermissionError, ValueError, TypeError):
                return {'status': 'blocked', 'error_code': 'repair_owner_unknown'}
            _update(alert_id, repair_status='failed', repair_error_code='repair_interrupted', repair_finished_at=state.now_iso())
            if alert.get('repair_run_id'):
                syncv2.finish_action_run(alert['repair_run_id'], 'interrupted', error_code='repair_interrupted')
            return {'status': 'blocked', 'error_code': 'repair_interrupted'}
        if automatic and not automatic_budget_available(alert):
            return {'status': 'blocked', 'error_code': 'automatic_repair_budget_exhausted'}
        try:
            require_index_ready()
        except memory.AutomationError as exc:
            pending = exc.code == 'memory_index_update_pending'
            _update(alert_id, repair_status=failure_repair_status(pending, int(alert.get('repair_attempt_count') or 0)),
                    repair_error_code=exc.code)
            return {'status': 'deferred' if pending else 'blocked', 'error_code': exc.code}
        run_id = syncv2.start_action_run('local_repair', trigger='automatic' if automatic else 'manual', requested_run_id=requested_run_id)
        _update(alert_id, repair_status='queued', repair_run_id=run_id, repair_pid=None, repair_error_code=None)
    # A detached interpreter can lose the native app's macOS directory-access
    # attribution.  Persist the request and let that same native supervisor own
    # the entire repair.  Neither an API server nor a new session does the work.
    awakened = True if automatic else wake_supervisor()
    return {'status': 'started', 'run_id': run_id, 'queued': True,
            'supervisor_wake_requested': awakened, 'fault_cleared': False}


def execute_queued(config_path: Path) -> dict[str, Any]:
    """Consume at most one allowlisted request in the supervisor process."""
    if maintenance_active():
        return {'status': 'deferred', 'error_code': 'maintenance_active'}
    with syncv2.file_lock(DISPATCH_LOCK, blocking=False):
        with state._connect(readonly=True) as connection:
            found = connection.execute("SELECT * FROM sync_alerts WHERE status='open' "
                                       "AND repair_status IN ('queued','running') "
                                       "ORDER BY CASE WHEN repair_status='running' THEN 0 ELSE 1 END,updated_at,alert_id LIMIT 1").fetchone()
            alert = dict(found) if found else {}
            if not alert:
                return {'status': 'idle'}
            run_id = alert.get('repair_run_id')
            run = connection.execute('SELECT action_type,status FROM action_runs WHERE run_id=?',
                                     (run_id,)).fetchone()
        try:
            uuid.UUID(str(run_id))
            valid_run = bool(run and run['action_type'] == 'local_repair' and run['status'] == 'running')
        except ValueError:
            valid_run = False
        if not eligible(alert) or not valid_run:
            _update(alert['alert_id'], repair_status='blocked', repair_error_code='repair_queue_invalid')
            return {'status': 'blocked', 'error_code': 'repair_queue_invalid'}
        try:
            alive = owner_alive(alert.get('repair_pid'))
        except (PermissionError, ValueError, TypeError):
            _update(alert['alert_id'], repair_error_code='repair_owner_unknown')
            return {'status': 'blocked', 'error_code': 'repair_owner_unknown'}
        if alive:
            return {'status': 'already_running', 'run_id': run_id}
        if alert.get('repair_pid') is not None or alert.get('repair_status') == 'running':
            _update(alert['alert_id'], repair_status='blocked', repair_error_code='repair_interrupted',
                    repair_finished_at=state.now_iso())
            syncv2.finish_action_run(run_id, 'interrupted', error_code='repair_interrupted')
            return {'status': 'blocked', 'error_code': 'repair_interrupted'}
        _update(alert['alert_id'], repair_pid=os.getpid())
    with supervisor_heartbeat():
        return execute(config_path, alert_id=alert['alert_id'], run_id=run_id, queued=True)


def perform(config: dict[str, Any], *, run_id: str | None = None, on_started: Callable[[], None] | None = None) -> dict[str, Any]:
    with ExitStack() as stack:
        stack.enter_context(controller.control_lock())
        stack.enter_context(memory.acquire_lock(memory.HOURLY_LOCK))
        stack.enter_context(memory.acquire_lock(memory.MEMORY_LOCK))
        memory.assert_validation_access(config)
        # Recheck under the lock: a new event can arrive after dispatch.  Defer
        # before on_started so neither a write nor an attempt is consumed.
        require_index_ready()
        if on_started:
            on_started()
        if run_id:
            syncv2.set_action_step(run_id, 'journals', 1, '恢复未收口回退记录', 'running')
        recovered = memory.recover_apply_journals(config, lock_held=True)
        conflicts = memory.recovery_conflicts(config)
        if run_id:
            syncv2.set_action_step(run_id, 'journals', 1, '恢复未收口回退记录', 'partial' if conflicts else 'success',
                                   detail={'recovered': recovered, 'conflicts': len(conflicts)})
            syncv2.set_action_step(run_id, 'validation', 2, '刷新索引并完整校验', 'running')
        result = memory.run_refresh_and_validate(config, lock_held=True, attempts=3)
        if run_id:
            syncv2.set_action_step(run_id, 'validation', 2, '刷新索引并完整校验', 'success')
        return {'status': 'partial' if conflicts else 'success', 'recovered_journals': recovered,
                'conflict_count': len(conflicts), 'validation_passed': True,
                'validation_attempts': result['validation_attempts'], 'fault_cleared': False}


def execute(config_path: Path, *, alert_id: str | None = None, run_id: str | None = None,
            maintenance: bool = False, queued: bool = False) -> dict[str, Any]:
    attempt_started = False
    count = 0
    try:
        state.initialize()
        if maintenance_active() and not maintenance:
            raise memory.AutomationError('maintenance_active', 'maintenance is active')
        if maintenance and not maintenance_active():
            raise memory.AutomationError('maintenance_required', 'deployment recovery requires maintenance')
        config = memory.load_config(config_path)
        def started() -> None:
            nonlocal attempt_started
            attempt_started = True
            if not alert_id:
                return
            with state._connect() as connection:
                connection.execute("UPDATE sync_alerts SET repair_status='running',repair_attempt_count=repair_attempt_count+1,repair_started_at=?,repair_pid=? WHERE alert_id=?",
                                   (state.now_iso(), os.getpid(), alert_id))
        result = perform(config, run_id=run_id, on_started=started)
        if queued:
            result.update(execution_mode='supervisor_in_process', execution_pid=os.getpid())
        if alert_id:
            _update(alert_id, repair_status='partial' if result['conflict_count'] else 'verified',
                    repair_finished_at=state.now_iso(), repair_error_code='recovery_conflict_requires_review' if result['conflict_count'] else None)
        if result['conflict_count']:
            state.open_alert('dingtalk_chat', 'recovery_conflict', '历史回退存在内容冲突',
                             '后来修改的业务文件已保留；仅继续处理来源与写入目标均独立的批次。', severity='error',
                             error_code='recovery_conflict_requires_review')
        if run_id:
            syncv2.finish_action_run(run_id, result['status'], result=result,
                                    error_code='recovery_conflict_requires_review' if result['conflict_count'] else None)
        return result
    except Exception as exc:
        code = str(getattr(exc, 'code', type(exc).__name__))
        deferred = code in {'already_running', 'maintenance_active', 'memory_index_update_pending', 'validation_inputs_changed'}
        result = {'status': 'deferred' if deferred else 'failed', 'error_code': code, 'fault_cleared': False}
        if alert_id:
            with state._connect(readonly=True) as connection:
                previous = connection.execute('SELECT repair_attempt_count FROM sync_alerts WHERE alert_id=?', (alert_id,)).fetchone()
            count = int(previous[0]) if previous else 1
            if queued and deferred and not attempt_started:
                # Keep the same unstarted request across index/lock waits.
                _update(alert_id, repair_status='queued', repair_pid=None, repair_error_code=code)
            else:
                _update(alert_id, repair_status=failure_repair_status(deferred, count), repair_error_code=code, repair_finished_at=state.now_iso())
        if run_id and not (queued and deferred and not attempt_started):
            syncv2.finish_action_run(run_id, result['status'], result=result, error_code=code)
        return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, default=ROOT / 'config.json')
    parser.add_argument('--alert-id')
    parser.add_argument('--run-id')
    parser.add_argument('--maintenance', action='store_true')
    args = parser.parse_args()
    result = execute(args.config, alert_id=args.alert_id, run_id=args.run_id, maintenance=args.maintenance)
    print(json.dumps(result))
    return 2 if result['status'] == 'failed' else 0


if __name__ == '__main__':
    import owner_retirement
    owner_retirement.require_running()
    raise SystemExit(main())
