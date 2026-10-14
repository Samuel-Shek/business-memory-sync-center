"""UI integration: waiting for evidence must not remain a false user action."""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch
import video_hold
import operational_workbench as work
import business_sync_state as state
from selftest_operational_workbench import fixture

with tempfile.TemporaryDirectory(prefix='listener-workbench-test-') as tmp:
    root=Path(tmp)
    with patch.object(video_hold,'DATABASE',root/'holds.sqlite'):
        app=fixture(root)
        alert=state.open_alert('dingtalk_chat','listener_failure','钉钉实时监听未完成验收','原始失败保留',error_code='event_retry_budget_exhausted')
        state.update_source('dingtalk_chat',status='manual_required',error_code='chat_reconcile_incomplete')
        work.reconcile(app.config)
        before=next(x for x in work.listing(app.config,include_business=False)['groups'] if x['error_code']=='event_retry_budget_exhausted')
        p=Path(app.config['external_root'])/'dingtalk-chat/state/automation_status.json';p.parent.mkdir(parents=True)
        p.write_text(json.dumps({'listener_state':'ready','listener_pid':os.getpid(),'direct_ready':True,'group_ready':True,'listener_ready_at':'2020-01-02T14:36:37+08:00','live_event_receipts':{'direct':{'received_at':'2020-01-02T17:33:00+08:00'}},'live_acceptance':'live_acceptance_pending','subscription_checked_at':'2020-01-02T17:34:00+08:00','incoming_by_kind':{'direct':True,'group':False}}))
        work.reconcile(app.config)
        after=next(x for x in work.listing(app.config,include_business=False)['groups'] if x['error_code']=='event_retry_budget_exhausted')
        assert before['group_id']==after['group_id']
        assert after['status']=='open' and after['attention_state']=='waiting',after
        assert '群聊' in after['reason'] and '重试上限' not in after['reason'],after
        assert '保持现有监听' in after['recovery_boundary'],after
        assert state.alerts(status='open')
        context=work.group_detail(app.config,after['group_id'])
        encoded=json.dumps(context,ensure_ascii=False)
        assert '等待群聊新消息验收' in encoded, encoded
        assert '原始失败保留' in encoded, encoded
print(json.dumps({'verdict':'pass','checks':8}))
