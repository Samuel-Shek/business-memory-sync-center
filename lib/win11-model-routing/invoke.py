"""Stable launcher; release switch is one atomic pointer, all code hash pinned."""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import hashlib,json,runpy,sys
from pathlib import Path
root=Path(__file__).resolve().parent
pointer=json.loads((root/'current.json').read_text('utf-8'))
release=root/'releases'/pointer['version']
if release.parent!=root/'releases':raise ValueError('invalid_release')
raw=(release/'manifest.json').read_bytes()
if hashlib.sha256(raw).hexdigest()!=pointer['manifest_sha256']:raise ValueError('manifest_mismatch')
manifest=json.loads(raw)
for name,expected in manifest['files'].items():
    if Path(name).name!=name or hashlib.sha256((release/name).read_bytes()).hexdigest()!=expected:raise ValueError('release_file_mismatch')
script=sys.argv.pop(1)
if script not in ('mail_route_runner','probe_routes','summary_cli'):raise ValueError('entrypoint_invalid')
sys.path.insert(0,str(release));runpy.run_path(str(release/(script+'.py')),run_name='__main__')
