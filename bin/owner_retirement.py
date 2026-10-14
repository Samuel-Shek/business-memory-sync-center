"""Read-only owner retirement gate. Unknown state seals corporate entrypoints."""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import json
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent

def stopped():
    try:
        c=json.loads((ROOT/"config.json").read_text())
        registry=json.loads((Path.home()/"AI/source-governance/retirements.json").read_text())
        return registry["business"]["active"] is not False or c.get("retirement_active") is not False
    except (OSError,ValueError,TypeError,KeyError,AttributeError):
        return True

def require_running():
    if stopped():
        print('{"status":"owner_retired","reason":"本人停用"}')
        raise SystemExit(0)
