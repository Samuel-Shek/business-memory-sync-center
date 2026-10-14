"""Offline checks using only synthetic records and temporary directories."""
import ast
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]

class ArchiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="sync-archive-test-")
        cls.root = Path(cls.temp.name)
        cls.previous = {k: os.environ.get(k) for k in ("SYNC_CENTER_HOME", "SYNC_CENTER_ENABLE_ARCHIVED_CODE")}
        os.environ["SYNC_CENTER_HOME"] = str(cls.root)
        os.environ["SYNC_CENTER_ENABLE_ARCHIVED_CODE"] = "1"
        sys.path.insert(0, str(REPO / "bin"))

    @classmethod
    def tearDownClass(cls):
        for key, value in cls.previous.items():
            if value is None: os.environ.pop(key, None)
            else: os.environ[key] = value
        cls.temp.cleanup()

    def test_default_import_is_disabled(self):
        env = os.environ.copy()
        env.pop("SYNC_CENTER_ENABLE_ARCHIVED_CODE", None)
        env["PYTHONPATH"] = str(REPO / "bin")
        result = subprocess.run([sys.executable, "-c", "import dingtalk_decision_center"], env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Archived integration is disabled", result.stderr)

    def test_all_python_source_parses(self):
        for path in REPO.rglob("*.py"):
            if "build" not in path.parts:
                with self.subTest(path=str(path.relative_to(REPO))):
                    ast.parse(path.read_text(encoding="utf-8"))

    def test_legacy_paths_stay_in_selected_root(self):
        paths = importlib.import_module("archive_paths")
        self.assertEqual(paths.path("/Users/example/AI/LocalData/BUSINESS-Memory-Bridge.nosync/state/test.json"), str(self.root / "state/test.json"))
        self.assertTrue(paths.path("/Users/example/Documents/example.txt").startswith(str(self.root / "isolated-home")))

    def test_integrity_detects_unauthorized_body_change(self):
        integrity = importlib.import_module("business_page_integrity")
        source = integrity.ensure_claim_base("虚构项目甲，待核验。\n")
        self.assertTrue(integrity.inspect_page(source).valid)
        self.assertFalse(integrity.inspect_page(source.replace("待核验", "已批准")).valid)
        changed = integrity.prepare_base_update(source, source.replace("待核验", "等待资料"), allow_base_change=True)
        self.assertTrue(integrity.inspect_page(changed).valid)

    def test_media_classification_is_offline(self):
        hold = importlib.import_module("video_hold")
        self.assertEqual(hold.classify("synthetic.mp4"), "video")
        self.assertIsNone(hold.classify("synthetic.txt"))
        self.assertEqual(hold.classify("unknown.zzzzz"), "unknown_media")

    def test_disabled_example_has_no_writers(self):
        cfg = json.loads((REPO / "config.example.json").read_text())
        self.assertTrue(cfg["retirement_active"])
        self.assertEqual(cfg["memory_writers"], [])
        self.assertFalse(cfg["email_sync"]["enabled"])

    def test_routing_requires_explicit_safe_rules(self):
        router = importlib.import_module("business_page_router")
        rules = self.root / "routing-rules.json"
        if rules.exists(): rules.unlink()
        with self.assertRaises(router.RoutingError): router.infer_target_page({"title": "synthetic"})
        rules.write_text(json.dumps([{"keywords":["synthetic"],"target_page":"客户及展会/示例客户/项目说明.md"}], ensure_ascii=False))
        self.assertEqual(router.infer_target_page({"title":"synthetic"}), "客户及展会/示例客户/项目说明.md")
        rules.write_text(json.dumps([{"keywords":["synthetic"],"target_page":"../../private.md"}]))
        with self.assertRaises(router.RoutingError): router.infer_target_page({"title":"synthetic"})

if __name__ == "__main__":
    unittest.main(verbosity=2)
