"""Isolated access recheck failure tests; no network or production writes."""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import unittest
from pathlib import Path
import subprocess,json
from contextlib import ExitStack
from unittest.mock import patch,Mock
import win11_mail_attachments as w
class RecheckCases(unittest.TestCase):
 def test_recheck_timeout_and_denial_are_distinct(self):
  session={'downloadUrl':'https://alimail-cn.aliyuncs.com/synthetic','attachment_body_requested_on_mac':False}
  for response,code in [(subprocess.TimeoutExpired('synthetic',90),'attachment_source_recheck_timeout'),(Mock(returncode=1,stdout=b'{}'),'content_source_probe_unavailable'),(Mock(returncode=0,stdout=b'invalid'),'content_source_probe_invalid')]:
   with self.subTest(code=code),ExitStack() as stack:
    mocks=[(w,'policy',{'worker_script_sha256':'x','ssh_host':'synthetic','expected_windows_host':'test'}),
      (w.content_registry,'mail_begin',{'policy':{}}),(w.content_registry,'mail_request',{}),
      (w,'_checked_file',Path('/synthetic')),(w,'_install_script','/synthetic'),(w,'_ssh',b'{}'),(w,'validate_result',{})]
    for obj,key,value in mocks:stack.enter_context(patch.object(obj,key,return_value=value))
    stack.enter_context(patch('video_hold.guard'))
    stack.enter_context(patch.object(w.subprocess,'run',side_effect=[Mock(returncode=0,stdout=json.dumps(session).encode()),response]))
    finish=stack.enter_context(patch.object(w.content_registry,'mail_finish'))
    with self.assertRaises(w.AttachmentError) as raised:
     w.fetch({}, {'message_key':'k','message_id':'m','mailbox':'fixture_1a905224@example.invalid'}, {'id':'a','name':'a.txt','size':1})
    self.assertEqual(raised.exception.code,code);finish.assert_not_called()

if __name__=='__main__':unittest.main(verbosity=2)
