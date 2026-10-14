
from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import unittest
import tempfile
import sqlite3
import json
import os
from pathlib import Path
import supervisor_worker_progress as progress

class ProgressTests(unittest.TestCase):
    def test_metadata_progress_never_implies_controller_completion(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t); db=root/"sync.sqlite"
            with sqlite3.connect(db) as c:
                c.execute("CREATE TABLE action_runs(run_id,action_type,started_at,updated_at)")
                c.execute("CREATE TABLE action_steps(run_id,step_key,updated_at,started_at,step_order)")
                c.execute("INSERT INTO action_runs VALUES('r','dingtalk_mail','2020-01-02T00:00:01+00:00','2020-01-02T00:00:01+00:00')")
                c.execute("INSERT INTO action_steps VALUES('r','email_sync','2020-01-02T00:00:01+00:00','2020-01-02T00:00:01+00:00',1)")
            (root/"state").mkdir()
            identifier='557a953a-7431-5891-bc7e-faa8543463b4'
            with sqlite3.connect(root/"state/email-sync.sqlite") as c:
                c.execute("CREATE TABLE semantic_groups(semantic_id,status,created_at)")
                c.execute("INSERT INTO semantic_groups VALUES(?,'pending','2020-01-02')",(identifier,))
            dest=root/"runs"/identifier/"model";dest.mkdir(parents=True)
            (dest/"batch-coverage.json").write_text(json.dumps({"status":"complete","files":[{"chunks":[{"status":"validated","chars":50}]}]}))
            # Raw input intentionally unreadable; reader has no reason to open it.
            (dest/"input.json").write_text("PRIVATE SOURCE CONTENT")
            os.chmod(dest/"input.json",0)
            result=progress.read(db,{"email_sync":{"state_root":str(root)}},"dingtalk_mail","2020-01-02T00:00:00+00:00")
            self.assertEqual(result["validated_chunks"],1)
            self.assertTrue(result["fragment_stage_complete"])
            self.assertNotIn("status",result)
            self.assertNotIn("PRIVATE",str(result))
            self.assertEqual(result["covered_chars"],50)

    def test_missing_database_is_unavailable_and_not_created(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/"missing.sqlite"
            result=progress.read(p,{},"win11","2020-01-02T00:00:00+00:00")
            self.assertEqual(result["progress"],"unavailable")
            self.assertFalse(p.exists())

if __name__=="__main__":unittest.main()
