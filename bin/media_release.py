"""Fence media entrypoints to the code actually loaded by this process."""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

from pathlib import Path
import hashlib
NAMES=('media_release.py','media_classification.py','media_image_intake.py','media_processor.py','media_actions.py','media_sources.py','image_frame_coverage.py')
DIRECTORY=Path(__file__).resolve().parent

def binding(directory=DIRECTORY):
    return {name:hashlib.sha256((directory/name).read_bytes()).hexdigest() for name in NAMES}

LOADED=binding()
class ReleaseChanged(RuntimeError):
    code='media_release_changed'

def check():
    if binding()!=LOADED:raise ReleaseChanged('媒体处理版本已更新，等待新进程接续。')
