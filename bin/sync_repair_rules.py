"""Registered pure rules. Fixed controller, tests and permissions live elsewhere."""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

def email_source(record, source_ref):
    return record.get('source_ref') == source_ref and record.get('memory_kind') == 'source_index' and record.get('source_system') == 'email'

def model_error(category, attempts):
    actual = [a for a in attempts if a.get('category') not in ('cooling_down', 'unconfigured')]
    if any(a.get('rejection_code') == 'proposal_source_drift' for a in actual):
        return 'model_source_changed'
    if actual and all(a.get('provider') == 'gpt' for a in actual):
        return {'quota':'codex_subscription_limit','auth':'codex_subscription_auth','timeout':'codex_timeout','bad_request':'codex_model_unavailable','invalid_output':'model_output_invalid'}.get(category, 'model_chain_exhausted')
    return {'quota':'model_quota_unavailable','auth':'model_auth_unavailable','timeout':'model_timeout','bad_request':'model_unavailable','invalid_output':'model_output_invalid'}.get(category, 'model_chain_exhausted')

def attachment_complete(reading):
    return reading.get('receipt_verified') is True and reading.get('status') == 'extracted'
