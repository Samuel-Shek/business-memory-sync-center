"""Synthetic-only tests. Run python3 -B selftest_mail_transaction_recovery.py.

No production imports, entrypoints, configuration, or business content are read.
All source/backup/journal fixtures are inside a fresh TemporaryDirectory.
"""

from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import copy
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

import mail_transaction_recovery as recovery


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mail-recovery-selftest-")
        self.addCleanup(self.temporary.cleanup)
        # macOS /var and /tmp may be system symlinks: canonicalize trusted fixture
        # root at setup, never normalize away a path supplied by the journal.
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "memory"
        self.backups = self.root / "run" / "backup"
        self.source.mkdir()
        self.backups.mkdir(parents=True)
        self.index = self.source / "derived-index.md"
        self.journal = {"schema_version": 2, "state": "applying", "paths": []}

    def safe_target(self, target):
        return target.parent == self.source

    def entry(self, name="note.md", before=b"before\n", after=b"after\n", derived=False):
        target = self.source / name
        if before is not None:
            target.write_bytes(before)
            target.chmod(0o640)
        backup = None
        if before is not None and not derived:
            backup = self.backups / name
            backup.write_bytes(before)
            backup.chmod(0o600)
        entry = recovery.make_entry(target, after, backup=backup, backup_root=self.backups,
                                    safe_target=self.safe_target, derived=derived)
        self.journal["paths"].append(entry)
        return target, entry

    def run_recovery(self, journal=None, **kwargs):
        return recovery.rollback_transaction(
            self.journal if journal is None else journal, backup_root=self.backups,
            safe_target=self.safe_target, derived_targets=[self.index],
            lock_held=kwargs.pop("lock_held", True), **kwargs)

    def snapshot(self):
        """Detect even same-byte replacement, chmod or mtime changes on conflict."""
        result = {}
        for path in sorted(self.root.rglob("*")):
            info = path.lstat()
            kind = stat.S_IFMT(info.st_mode)
            content = (os.readlink(path) if path.is_symlink() else
                       path.read_bytes() if path.is_file() else None)
            result[str(path.relative_to(self.root))] = (
                kind, stat.S_IMODE(info.st_mode), info.st_ino, info.st_mtime_ns, content)
        return result

    def assert_conflict_untouched(self, code=None, journal=None, **kwargs):
        before = self.snapshot()
        original = copy.deepcopy(self.journal if journal is None else journal)
        result = self.run_recovery(journal, **kwargs)
        self.assertEqual(result["status"], "conflict", result)
        self.assertEqual(result["changed"], [])
        if code:
            self.assertIn(code, [item["code"] for item in result["conflicts"]])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.journal if journal is None else journal, original)
        return result

    def test_entry_hashes_and_constructor_read_only(self):
        target, entry = self.entry(before=b"\x00\xff\r\n", after=b"new\r\n")
        self.assertEqual(entry["before_sha256"], recovery.sha256_bytes(b"\x00\xff\r\n"))
        self.assertEqual(entry["after_sha256"], recovery.sha256_bytes(b"new\r\n"))
        self.assertEqual(entry["backup_sha256"], entry["before_sha256"])
        before = self.snapshot()
        self.assertEqual(recovery.make_entry(target, b"new\r\n", backup=Path(entry["backup"]),
                         backup_root=self.backups, safe_target=self.safe_target), entry)
        self.assertEqual(self.snapshot(), before)

    def test_restore_binary_bytes_and_original_mode(self):
        target, _ = self.entry(before=b"\x00\xff\r\n")
        target.write_bytes(b"after\n")
        target.chmod(0o600)
        result = self.run_recovery()
        self.assertEqual(result["status"], "restored")
        self.assertEqual(target.read_bytes(), b"\x00\xff\r\n")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)

    def test_later_writer_blocks_old_backup(self):
        target, _ = self.entry()
        target.write_bytes(b"later task's content")
        self.assert_conflict_untouched("current_hash_conflict")

    def test_mixed_entries_full_preflight_zero_writes(self):
        first, _ = self.entry("a.md")
        first.write_bytes(b"after\n")
        new, _ = self.entry("b.md", before=None)
        new.write_bytes(b"after\n")
        last, _ = self.entry("z.md")
        last.write_bytes(b"later task")
        with patch.object(recovery, "_atomic_restore") as writer, \
                patch.object(recovery.tempfile, "mkstemp") as temp_writer, \
                patch.object(Path, "unlink") as remover:
            self.assert_conflict_untouched("current_hash_conflict")
            writer.assert_not_called()
            temp_writer.assert_not_called()
            remover.assert_not_called()

    def test_collects_all_conflicts(self):
        a, _ = self.entry("a.md")
        b, _ = self.entry("b.md")
        a.write_bytes(b"other A")
        b.write_bytes(b"other B")
        self.assertEqual(len(self.assert_conflict_untouched()["conflicts"]), 2)

    def test_schema1_and_missing_hashes_refused(self):
        target, entry = self.entry()
        target.write_bytes(b"after\n")
        legacy = {"schema_version": 1, "state": "applying", "paths": [
            {k: v for k, v in entry.items() if k in {"target", "backup", "existed", "mode"}}]}
        self.assert_conflict_untouched("legacy_or_unsupported_schema", legacy)
        legacy["schema_version"] = 2
        self.assert_conflict_untouched("missing_schema2_fields", legacy)

    def test_repeated_active_recovery_no_second_writes(self):
        target, _ = self.entry()
        target.write_bytes(b"after\n")
        self.assertEqual(self.run_recovery()["changed"], [str(target)])
        before = self.snapshot()
        self.assertEqual(self.run_recovery()["changed"], [])
        self.assertEqual(self.snapshot(), before)

    def test_before_state_is_no_change_including_metadata(self):
        self.entry()
        before = self.snapshot()
        self.assertEqual(self.run_recovery()["changed"], [])
        self.assertEqual(self.snapshot(), before)

    def test_new_file_deleted_only_for_owned_after(self):
        target, entry = self.entry(before=None)
        self.assertIsNone(entry["before_sha256"])
        target.write_bytes(b"after\n")
        self.assertEqual(self.run_recovery()["status"], "restored")
        self.assertFalse(target.exists())
        self.assertEqual(self.run_recovery()["changed"], [])

    def test_new_file_later_content_not_deleted(self):
        target, _ = self.entry(before=None)
        target.write_bytes(b"later task")
        self.assert_conflict_untouched("current_hash_conflict")

    def test_missing_and_empty_are_different(self):
        target, entry = self.entry(before=b"", after=b"nonempty")
        self.assertEqual(entry["before_sha256"], recovery.sha256_bytes(b""))
        target.unlink()
        self.assert_conflict_untouched("current_hash_conflict")

    def test_transaction_owned_deletion_can_be_restored(self):
        target, _ = self.entry(after=None)
        target.unlink()
        self.assertEqual(self.run_recovery()["status"], "restored")
        self.assertEqual(target.read_bytes(), b"before\n")

    def test_backup_pollution_blocks_entire_batch(self):
        a, _ = self.entry("a.md")
        b, entry = self.entry("b.md")
        a.write_bytes(b"after\n")
        b.write_bytes(b"after\n")
        Path(entry["backup"]).write_bytes(b"polluted")
        self.assert_conflict_untouched("backup_hash_mismatch")

    def test_backup_hash_cannot_be_rebound_independently(self):
        target, entry = self.entry()
        target.write_bytes(b"after\n")
        Path(entry["backup"]).write_bytes(b"polluted")
        entry["backup_sha256"] = recovery.sha256_bytes(b"polluted")
        self.assert_conflict_untouched("backup_hash_mismatch")

    def test_missing_backup_even_if_already_restored_is_refused(self):
        _, entry = self.entry()
        Path(entry["backup"]).unlink()
        self.assert_conflict_untouched("backup_hash_mismatch")

    def test_constructor_refuses_polluted_backup_without_writes(self):
        target, entry = self.entry()
        backup = Path(entry["backup"])
        backup.write_bytes(b"wrong")
        before = self.snapshot()
        with self.assertRaises(recovery.RecoveryConflict):
            recovery.make_entry(target, b"after", backup=backup, backup_root=self.backups,
                                safe_target=self.safe_target)
        self.assertEqual(self.snapshot(), before)

    def test_derived_is_only_returned_for_rebuild(self):
        target, _ = self.entry()
        index, entry = self.entry("derived-index.md", b"old index", b"new index", derived=True)
        self.assertIsNone(entry["backup"])
        target.write_bytes(b"after\n")
        index.write_bytes(b"new index")
        journal_copy = copy.deepcopy(self.journal)
        result = self.run_recovery()
        self.assertEqual(result["changed"], [str(target)])
        self.assertEqual(result["rebuild_required"], [str(index)])
        self.assertEqual(index.read_bytes(), b"new index")
        self.assertEqual(self.journal, journal_copy)
        self.assertEqual(result["next_state"], "restored_pending_validation")

    def test_later_derived_index_change_blocks_source_rollback(self):
        target, _ = self.entry()
        index, _ = self.entry("derived-index.md", derived=True)
        target.write_bytes(b"after\n")
        index.write_bytes(b"later writer's index")
        self.assert_conflict_untouched("current_hash_conflict")

    def test_derived_backup_and_forged_classification_rejected(self):
        _, entry = self.entry()
        entry["derived"] = True
        self.assert_conflict_untouched("derived_allowlist_mismatch")
        self.journal["paths"].clear()
        _, entry = self.entry("derived-index.md", derived=True)
        entry["backup"] = str(self.backups / "old-index")
        self.assert_conflict_untouched("unexpected_backup")

    def test_index_cannot_be_restored_as_source(self):
        self.entry("derived-index.md")
        self.assert_conflict_untouched("derived_allowlist_mismatch")

    def test_unsafe_target_outside_allowlist(self):
        _, entry = self.entry()
        outside = self.root / "outside.md"
        outside.write_bytes(b"after\n")
        entry["target"] = str(outside)
        self.assert_conflict_untouched("unsafe_target")

    def test_relative_and_dotdot_paths_rejected(self):
        _, entry = self.entry()
        for bad in ("note.md", str(self.source / ".." / "memory" / "note.md")):
            entry["target"] = bad
            self.assert_conflict_untouched("unsafe_path")

    def test_target_symlink_and_dangling_symlink_rejected(self):
        target, _ = self.entry()
        other = self.root / "other.md"
        other.write_bytes(b"after\n")
        target.unlink()
        target.symlink_to(other)
        self.assert_conflict_untouched("unsafe_path")
        other.unlink()
        self.assert_conflict_untouched("unsafe_path")

    def test_symlinked_parent_even_with_permissive_predicate(self):
        _, entry = self.entry()
        link = self.root / "linked-memory"
        link.symlink_to(self.source, target_is_directory=True)
        entry["target"] = str(link / "note.md")
        self.safe_target = lambda p: True
        self.assert_conflict_untouched("unsafe_path")

    def test_backup_outside_root_and_symlink_rejected(self):
        _, entry = self.entry()
        original = Path(entry["backup"])
        other = self.root / "other-backup"
        other.write_bytes(b"before\n")
        entry["backup"] = str(other)
        self.assert_conflict_untouched("unsafe_backup")
        entry["backup"] = str(original)
        original.unlink()
        original.symlink_to(other)
        self.assert_conflict_untouched("unsafe_path")

    def test_backup_parent_symlink_and_sibling_prefix_escape(self):
        _, entry = self.entry()
        sibling = self.backups.with_name("backup-other")
        sibling.mkdir()
        other = sibling / "note.md"
        other.write_bytes(b"before\n")
        entry["backup"] = str(other)
        self.assert_conflict_untouched("unsafe_backup")
        link = self.backups / "link"
        link.symlink_to(sibling, target_is_directory=True)
        entry["backup"] = str(link / "note.md")
        self.assert_conflict_untouched("unsafe_path")

    def test_hardlink_and_nonregular_target_rejected(self):
        target, _ = self.entry()
        other = self.root / "hardlink"
        os.link(target, other)
        self.assert_conflict_untouched("not_private_regular_file")
        other.unlink()
        target.unlink()
        target.mkdir()
        self.assert_conflict_untouched()

    def test_duplicate_targets_or_backups_rejected(self):
        _, entry = self.entry()
        self.journal["paths"].append(copy.deepcopy(entry))
        self.assert_conflict_untouched("duplicate_path")
        self.journal["paths"].pop()
        _, second = self.entry("second.md")
        second["backup"] = entry["backup"]
        self.assert_conflict_untouched("duplicate_path")

    def test_backup_tree_cannot_be_target(self):
        _, entry = self.entry()
        entry["target"] = entry["backup"]
        self.safe_target = lambda p: True
        self.assert_conflict_untouched("unsafe_target")

    def test_invalid_fields_after_valid_entry_still_zero_writes(self):
        target, _ = self.entry("a.md")
        _, second = self.entry("b.md")
        target.write_bytes(b"after\n")
        for key, value in (("mode", True), ("mode", 0o4777), ("existed", "yes"),
                           ("before_sha256", []), ("after_sha256", "x"), ("derived", 1)):
            original = second[key]
            second[key] = value
            self.assert_conflict_untouched()
            second[key] = original

    def test_lock_assertion_required(self):
        target, _ = self.entry()
        target.write_bytes(b"after\n")
        self.assert_conflict_untouched("exclusive_lock_required", lock_held=False)

    def test_schema2_json_round_trip_for_restart(self):
        target, _ = self.entry()
        target.write_bytes(b"after\n")
        saved = self.root / "run" / "transaction.json"
        saved.write_text(json.dumps(self.journal), encoding="utf-8")
        before_journal = saved.read_bytes()
        loaded = json.loads(saved.read_text(encoding="utf-8"))
        self.assertEqual(self.run_recovery(loaded)["status"], "restored")
        self.assertEqual(saved.read_bytes(), before_journal)

    def test_terminal_journals_never_revert_later_writes(self):
        target, _ = self.entry()
        target.write_bytes(b"later task")
        before = self.snapshot()
        for state in ("committed", "rolled_back", "rolled_back_after_recovery"):
            self.journal["state"] = state
            self.assertEqual(self.run_recovery()["status"], "noop")
        self.assertEqual(self.snapshot(), before)

    def test_pending_validation_no_source_rollback(self):
        target, _ = self.entry()
        self.journal["state"] = "restored_pending_validation"
        self.assertEqual(self.run_recovery()["changed"], [])
        target.write_bytes(b"after\n")
        self.assert_conflict_untouched("pending_validation_source_changed")

    def test_interrupted_partial_rollback_is_retryable(self):
        a, _ = self.entry("a.md")
        b, _ = self.entry("b.md")
        a.write_bytes(b"after\n")
        b.write_bytes(b"after\n")
        real = recovery._atomic_restore

        def fail_second(target, data, mode):
            if target == b:
                raise OSError("synthetic I/O failure")
            return real(target, data, mode)

        with patch.object(recovery, "_atomic_restore", side_effect=fail_second):
            with self.assertRaises(recovery.RecoveryWriteError) as caught:
                self.run_recovery()
        self.assertEqual(caught.exception.completed, (str(a),))
        self.assertEqual(a.read_bytes(), b"before\n")
        self.assertEqual(b.read_bytes(), b"after\n")
        self.assertEqual(self.journal["state"], "applying")
        self.assertEqual(self.run_recovery()["changed"], [str(b)])

    def test_directory_sync_failure_is_not_misreported_as_conflict(self):
        target, _ = self.entry()
        target.write_bytes(b"after\n")
        with patch.object(recovery, "_sync_parent", side_effect=OSError("synthetic fsync failure")):
            with self.assertRaises(recovery.RecoveryWriteError) as caught:
                self.run_recovery()
        self.assertEqual(caught.exception.completed, (str(target),))
        self.assertEqual(self.journal["state"], "applying")
        self.assertEqual(target.read_bytes(), b"before\n")
        self.assertEqual(self.run_recovery()["changed"], [])

    def test_all_invalid_journal_shapes_are_read_only(self):
        self.entry()
        for journal in (None, [], {}, {"schema_version": True},
                        {"schema_version": 2, "state": []},
                        {"schema_version": 2, "state": "unknown"},
                        {"schema_version": 2, "state": "applying", "paths": []},
                        {"schema_version": 2, "state": "applying", "paths": [None]}):
            # None normally means default journal in test wrapper; call directly.
            before = self.snapshot()
            result = recovery.rollback_transaction(journal, backup_root=self.backups,
                     safe_target=self.safe_target, lock_held=True)
            self.assertEqual(result["status"], "conflict")
            self.assertEqual(self.snapshot(), before)

    def test_source_only_journal_allows_later_derived_index_updates(self):
        """Chosen integration: only original changes are owned rollback targets."""
        target, _ = self.entry()
        target.write_bytes(b"after\n")
        indexes = [self.index, self.source / "pending-index.md", self.source / "search-index.md"]
        for index in indexes:
            index.write_bytes(b"index from a later task; no owned after hash")
        self.journal["derived_indexes"] = [str(p) for p in indexes]
        self.assertEqual(len(self.journal["paths"]), 1)
        journal_before = copy.deepcopy(self.journal)
        index_before = {p: (p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns) for p in indexes}
        source_only_check = lambda p: self.safe_target(p) and p not in indexes
        for expected_changes in ([str(target)], []):
            result = recovery.rollback_transaction(
                self.journal, backup_root=self.backups, safe_target=source_only_check,
                derived_targets=set(), lock_held=True)
            self.assertEqual(result["status"], "restored")
            self.assertEqual(result["changed"], expected_changes)
            # Top-level derived_indexes is owned/validated by the entrypoint.
            # Its rebuild MUST NOT depend on this helper field being nonempty.
            self.assertEqual(result["rebuild_required"], [])
        self.assertEqual(target.read_bytes(), b"before\n")
        self.assertEqual(self.journal, journal_before)
        self.assertEqual({p: (p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns)
                          for p in indexes}, index_before)

    def test_source_only_integration_rejects_index_smuggled_into_paths(self):
        target, _ = self.entry()
        target.write_bytes(b"after\n")
        self.entry("derived-index.md")  # forged ordinary entry with an old backup
        before = self.snapshot()
        result = recovery.rollback_transaction(
            self.journal, backup_root=self.backups,
            safe_target=lambda p: self.safe_target(p) and p != self.index,
            derived_targets=set(), lock_held=True)
        self.assertEqual(result["status"], "conflict")
        self.assertIn("unsafe_target", [c["code"] for c in result["conflicts"]])
        self.assertEqual(self.snapshot(), before)

    def test_source_only_conflict_does_not_touch_top_level_indexes(self):
        first, _ = self.entry("first.md")
        later, _ = self.entry("later.md")
        first.write_bytes(b"after\n")
        later.write_bytes(b"later writer's source")
        self.index.write_bytes(b"later writer's index")
        self.journal["derived_indexes"] = [str(self.index)]
        before = self.snapshot()
        result = recovery.rollback_transaction(
            self.journal, backup_root=self.backups,
            safe_target=lambda p: self.safe_target(p) and p != self.index,
            derived_targets=set(), lock_held=True)
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(self.snapshot(), before)

    def test_source_only_entrypoint_rebuild_failure_and_resume(self):
        """Synthetic state sequencing only; production refresh is NEVER imported."""
        target, _ = self.entry()
        target.write_bytes(b"after\n")
        self.index.write_bytes(b"old derived")
        self.journal["derived_indexes"] = [str(self.index)]
        result = recovery.rollback_transaction(
            self.journal, backup_root=self.backups,
            safe_target=lambda p: self.safe_target(p) and p != self.index,
            derived_targets=set(), lock_held=True)
        self.assertEqual(result["status"], "restored")
        journal_path = self.root / "run" / "transaction.json"
        self.journal["state"] = result["next_state"]
        journal_path.write_text(json.dumps(self.journal), encoding="utf-8")

        def synthetic_failed_refresh():
            self.index.write_bytes(b"partially rebuilt derived")
            raise OSError("synthetic validation failure")

        with self.assertRaises(OSError):
            synthetic_failed_refresh()
        restarted = json.loads(journal_path.read_text(encoding="utf-8"))
        self.assertEqual(restarted["state"], "restored_pending_validation")
        # Entry point resumes refresh only: later source changes are retained.
        target.write_bytes(b"later writer after source rollback")
        self.index.write_bytes(b"rebuilt: " + target.read_bytes())
        restarted["state"] = "rolled_back_after_recovery"
        journal_path.write_text(json.dumps(restarted), encoding="utf-8")
        before = self.snapshot()
        result = recovery.rollback_transaction(
            restarted, backup_root=self.backups,
            safe_target=lambda p: self.safe_target(p) and p != self.index,
            derived_targets=set(), lock_held=True)
        self.assertEqual(result["status"], "noop")
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
