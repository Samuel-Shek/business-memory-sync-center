#!/usr/bin/env python3
"""Retired by owner on 2020-01-02; company Windows returned."""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import sys
if __name__ == '__main__':
    print('公司 Windows 连接已撤除；旧挂载管理器永久停用。', file=sys.stderr)
    raise SystemExit(64)
