"""Read and validate changed pure rules before any caller uses them."""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import hashlib
from pathlib import Path
import sync_repair_contract
_cache={}
def call(name,*args):
    data=Path(__file__).with_name('sync_repair_rules.py').read_bytes()
    key=hashlib.sha256(data).hexdigest()
    if _cache.get('sha256')!=key:
        text=data.decode()
        if not sync_repair_contract.check(text)['passed']:raise ValueError('sync_rule_contract_failed')
        _cache.update(sha256=key,functions=sync_repair_contract.load(text))
    return _cache['functions'][name](*args)
def email_source(record,ref):return call('email_source',record,ref)
def model_error(category,attempts):return call('model_error',category,attempts)
def attachment_complete(reading):return call('attachment_complete',reading)
