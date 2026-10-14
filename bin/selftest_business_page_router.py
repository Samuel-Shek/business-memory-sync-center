#!/usr/bin/env python3
"""Offline routing checks with entirely synthetic records."""
import json
import os
from pathlib import Path
import tempfile
import unittest

class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='synthetic-router-')
        self.root = Path(self.temp.name)
        self.previous = {key: os.environ.get(key) for key in ('SYNC_CENTER_HOME', 'SYNC_CENTER_ENABLE_ARCHIVED_CODE')}
        os.environ.update(SYNC_CENTER_HOME=str(self.root), SYNC_CENTER_ENABLE_ARCHIVED_CODE='1')
        import business_page_router
        self.router = business_page_router

    def tearDown(self):
        for key, value in self.previous.items():
            if value is None: os.environ.pop(key, None)
            else: os.environ[key] = value
        self.temp.cleanup()

    def test_missing_rules_cannot_route(self):
        with self.assertRaises(self.router.RoutingError):
            self.router.infer_target_page({'title': 'synthetic-project'})

    def test_explicit_rules_and_no_match(self):
        rules = [{'keywords': ['synthetic-project'], 'target_page': '客户及展会/虚构客户/项目说明.md'}]
        (self.root / 'routing-rules.json').write_text(json.dumps(rules, ensure_ascii=False))
        self.assertEqual(self.router.infer_target_page({'title': 'synthetic-project'}), rules[0]['target_page'])
        with self.assertRaises(self.router.RoutingError):
            self.router.infer_target_page({'title': 'unrelated-fixture'})

    def test_unsafe_target_rejected(self):
        with self.assertRaises(self.router.RoutingError):
            self.router.safe_relative_page('../../secret.md')
        with self.assertRaises(self.router.RoutingError):
            self.router.safe_relative_page('.hidden/private.md')

if __name__ == '__main__':
    unittest.main(verbosity=2)
