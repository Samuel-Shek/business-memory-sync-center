#!/usr/bin/env python3
"""Bounded attachment child of the existing email controller."""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import argparse
import os
import ssl
from pathlib import Path
import email_sync
import email_phases
p=argparse.ArgumentParser();p.add_argument('--config',type=Path,required=True);p.add_argument('--kind',choices=['image','win11'],required=True)
if __name__=='__main__':
    import owner_retirement
    owner_retirement.require_running()
    # Framework Python may lack its CA bundle; use the same verified system trust as /usr/bin/python3.
    if not os.environ.get('SSL_CERT_FILE') and ssl.get_default_verify_paths().cafile is None and Path('/etc/ssl/cert.pem').is_file():
        os.environ['SSL_CERT_FILE']='/etc/ssl/cert.pem'
    a=p.parse_args()
    try:email_phases.worker(email_sync.load_config(a.config),a.kind)
    except Exception as exc:
        import json
        config=json.loads(a.config.read_text())
        path=email_sync.runtime_paths(config)['state_root']/'state'/('phase-'+a.kind+'-startup-error.json')
        email_sync.atomic_write_text(path,json.dumps({'at':email_sync.now_iso(),'error_code':getattr(exc,'code',type(exc).__name__)}))
        raise SystemExit(1)
