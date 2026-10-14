"""Mail attachment policy and SSH transport. Never fetch original bytes on M2."""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import base64
from email.utils import parseaddr
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time
import uuid
import sqlite3
import content_registry
from content_identity import AccessDenied, cache_key, key
from urllib.parse import urlsplit

POLICY_ID = 'win11-direct-except-self-v1'
ORIGINS = {'alimail-cn.aliyuncs.com', 'alimail-personal.aliyuncs.com'}
SHA = re.compile(r'^[0-9a-f]{64}$')


class AttachmentError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def address(value):
    if isinstance(value, dict):
        value = value.get('email') or value.get('address') or value.get('emailAddress')
        if isinstance(value, dict):
            value = value.get('address')
    if not isinstance(value, str):
        return ''
    _, addr = parseaddr(value.strip())
    return addr.casefold() if re.fullmatch(r'[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+', addr) else ''


def policy(config):
    value = (config.get('email_sync') or {}).get('attachment_processing')
    if value is None:
        return None
    if not isinstance(value, dict) or value.get('policy_id') != POLICY_ID:
        raise AttachmentError('attachment_policy_invalid')
    owned = value.get('self_sender_addresses')
    if (not isinstance(owned, list) or not owned or any(not address(v) or address(v) != v for v in owned)
            or address(config['email_sync']['mailbox']) not in owned
            or value.get('ssh_host') != 'win-main' or value.get('expected_windows_host') != 'EXAMPLE_USER'):
        raise AttachmentError('attachment_policy_invalid')
    for key in ['session_helper', 'worker_script', 'dispatch_script']:
        if not isinstance(value.get(key), str) or not Path(value[key]).is_absolute() or not SHA.fullmatch(str(value.get(key + '_sha256', ''))):
            raise AttachmentError('attachment_policy_invalid')
    return value


def sender_is_self(message, value):
    sender = address(message.get('from'))
    if not sender:
        raise AttachmentError('sender_identity_missing')
    return sender in value['self_sender_addresses']


def policy_receipt(message, value):
    return {'policy_id': POLICY_ID, 'sender_sha256': digest(address(message.get('from')).encode()),
            'self_sender_sha256': [digest(v.encode()) for v in value['self_sender_addresses']],
            'originals_on_m2': False}


def _checked_file(value, key):
    path = Path(value[key])
    if path.is_symlink() or not path.is_file() or digest(path.read_bytes()) != value[key + '_sha256']:
        raise AttachmentError('attachment_worker_binding_changed')
    return path


def _ssh(host, code, *, payload=None, timeout=30):
    encoded = base64.b64encode(code.encode('utf-16le')).decode('ascii')
    try:
        result = subprocess.run(['/usr/bin/ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15',
                '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2', host,
                'powershell -NoProfile -EncodedCommand ' + encoded],
                input=payload, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AttachmentError('win11_network_unavailable') from exc
    if result.returncode:
        if b'Permission denied (publickey)' in result.stderr:
            raise AttachmentError('win11_authentication_failed')
        if result.returncode != 255:
            raise AttachmentError('win11_remote_command_failed')
        raise AttachmentError('win11_network_unavailable')
    return result.stdout


POWER_PROBE = ("Add-Type -AssemblyName System.Windows.Forms;"
               "[System.Windows.Forms.SystemInformation]::PowerStatus.PowerLineStatus.ToString()")
_POWER_CACHE = {}


def example_bd50c44d55d3(config, *, max_age=300):
    """Whether the Win11 attachment reader is plugged in (2020-01-02 Example Owner:
    large attachments are Win11's job while it is on power). Unreachable or
    unknown counts as not ready, so the mail simply waits for the next round."""
    value = policy(config)
    if not value:
        return False
    cached = _POWER_CACHE.get(value['ssh_host'])
    if cached and time.monotonic() - cached[0] < max_age:
        return cached[1]
    try:
        ready = _ssh(value['ssh_host'], POWER_PROBE, timeout=30).decode('utf-8', 'replace').strip() == 'Online'
    except AttachmentError:
        ready = False
    _POWER_CACHE[value['ssh_host']] = (time.monotonic(), ready)
    return ready


def _install_script(value, key):
    worker = _checked_file(value, key)
    script_hash = value[key + '_sha256']
    remote = 'C:/Users/example/AppData/Local/BusinessMemory/mail-attachments/workers/' + script_hash + '.ps1'
    check = "$p='" + remote + "';if(Test-Path -LiteralPath $p){(Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash.ToLowerInvariant()}"
    if _ssh(value['ssh_host'], check).decode().strip() == script_hash:
        return remote
    _ssh(value['ssh_host'], "$p='" + remote + "';[IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($p))|Out-Null")
    try:
        result = subprocess.run(['/usr/bin/scp', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', str(worker), value['ssh_host'] + ':' + remote], capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AttachmentError('win11_network_unavailable') from exc
    if result.returncode or _ssh(value['ssh_host'], check).decode().strip() != script_hash:
        raise AttachmentError('attachment_worker_binding_changed')
    return remote


def validate_result(result, *, job_id, identity, worker_hash, declared_size, expected_host):
    if not isinstance(result, dict) or result.get('downloaded') is not True:
        code = result.get('error_code', 'win11_download_failed') if isinstance(result, dict) else 'win11_receipt_invalid'
        raise AttachmentError(code if re.fullmatch(r'[a-z0-9_]{1,80}', str(code)) else 'win11_receipt_invalid')
    if (result.get('job_id') != job_id or result.get('identity_sha256') != identity
            or result.get('worker_sha256') != worker_hash or result.get('host') != expected_host
            or result.get('original_received_on_m2') is not False
            or not SHA.fullmatch(str(result.get('source_sha256', '')))
            or not isinstance(result.get('source_bytes'), int) or not 0 <= result['source_bytes'] <= 52428800
            or declared_size > 0 and result['source_bytes'] != declared_size
            or result.get('foreground_input_used') is not False or result.get('clipboard_used') is not False):
        raise AttachmentError('win11_receipt_identity_mismatch')
    path = str(result.get('remote_path', '')).replace('\\', '/')
    if not path.startswith('C:/Users/example/AppData/Local/BusinessMemory/mail-attachments/jobs/' + job_id + '/original.'):
        raise AttachmentError('win11_receipt_path_mismatch')
    text = result.get('text', '')
    if not isinstance(text, str):
        raise AttachmentError('win11_text_invalid')
    if result.get('schema_version') == 2:
        coverage = result.get('coverage')
        if (not isinstance(coverage, dict) or coverage.get('content_truncated') is not False
                or not isinstance(coverage.get('nodes'), list) or not coverage['nodes']
                or not SHA.fullmatch(str(result.get('reader_sha256', '')))
                or digest(text.encode('utf-8')) != result.get('text_sha256')
                or len(text.encode('utf-16le')) // 2 != result.get('text_chars')
                or len(text.encode('utf-8')) != result.get('text_bytes')):
            raise AttachmentError('win11_coverage_invalid')
        if result.get('local_view_sha256') != result.get('source_sha256'):
            binding = result.get('source_binding') or {}
            if (binding.get('method') != 'windows_original_container_and_authorized_read_view'
                    or binding.get('raw_sha256') != result.get('source_sha256')
                    or binding.get('raw_bytes') != result.get('source_bytes')
                    or binding.get('view_sha256') != result.get('local_view_sha256')
                    or not SHA.fullmatch(str(binding.get('view_sha256', '')))
                    or binding.get('view_bytes') != coverage['nodes'][0].get('bytes')
                    or coverage['nodes'][0].get('sha256') != binding.get('view_sha256')):
                raise AttachmentError('win11_source_view_unbound')
        elif (coverage['nodes'][0].get('sha256') != result['source_sha256']
                or coverage['nodes'][0].get('bytes') != result['source_bytes']):
            raise AttachmentError('win11_source_view_unbound')
        complete = all(n.get('status') in {'complete', 'excluded_self_sender'} for n in coverage['nodes'])
        if coverage.get('complete') is not complete:
            raise AttachmentError('win11_coverage_invalid')
        for node in coverage['nodes']:
            if (node.get('status')=='complete' and node.get('method')=='ooxml_all_content_parts_media_and_embeddings'
                and not node.get('content_parts')):
                raise AttachmentError('office_content_not_recognized')
            if node.get('verified_empty_workbook') is True and (node.get('nonempty_cells')!=0 or not node.get('sheets_expected') or node.get('sheets_read')!=node['sheets_expected']):
                raise AttachmentError('office_empty_workbook_unproven')
            for expected, actual in [('pages_expected','pages_read'),('frames_expected','frames_read'),('entries_expected','entries_read'),('sheets_expected','sheets_read')]:
                if complete and expected in node and node.get(expected) != node.get(actual):
                    raise AttachmentError('win11_coverage_invalid')
        chunks = [{'index':i//24000,'chars':len(text[i:i+24000]),'sha256':digest(text[i:i+24000].encode())} for i in range(0,len(text),24000)]
        if coverage.get('text_chunks') != chunks or coverage.get('text_chunk_size') != 24000:
            raise AttachmentError('win11_coverage_invalid')
        expected_status = 'extracted' if complete else 'incomplete_content'
        expected_risk = 'ordinary' if complete else 'unknown'
        if result.get('extract_status') != expected_status or result.get('risk') != expected_risk:
            raise AttachmentError('win11_coverage_invalid')
        return result
    if result.get('extract_status') == 'extracted':
        if (not text.strip() or digest(text.encode('utf-8')) != result.get('text_sha256')
                or len(text.encode('utf-16le')) // 2 != result.get('text_chars') or result.get('risk') != 'ordinary'):
            raise AttachmentError('win11_text_hash_mismatch')
    elif text or result.get('text_chars') != 0 or result.get('risk') != 'unknown':
        raise AttachmentError('win11_failure_receipt_invalid')
    return result


def fetch(config, row, item):
    import video_hold
    value = policy(config)
    try: handle = content_registry.mail_begin(config,row,item)
    except AccessDenied as exc: raise AttachmentError('content_source_access_denied') from exc
    except (OSError,ValueError) as exc: raise AttachmentError('content_policy_unavailable') from exc
    video_hold.guard('dingtalk_mail', {'message_key':row['message_key'],'attachment_id':digest(str(item['id']).encode())[:16]},
        name=item.get('name',''),mime=item.get('contentType') or item.get('mimeType',''),size=item.get('size'),parent=dict(row).get('subject',''))
    helper = _checked_file(value, 'session_helper')
    _install_script(value, 'worker_script')
    dispatch = _install_script(value, 'dispatch_script')
    # This helper returns a session before its HTTP body request. Its executable
    # is separate from the installed production DWS and bound by SHA-256.
    try:
        command = [str(helper), 'mail', 'attachment', 'download', '--email', str(row['mailbox']),
            '--message-id', str(row['message_id']), '--attachment-id', str(item['id']),
            '--name', str(item.get('name') or 'attachment'), '--session-only', '--format', 'json']
        response = subprocess.run(command, capture_output=True, timeout=90)
        session = json.loads(response.stdout) if response.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise AttachmentError('attachment_session_unavailable') from exc
    if not isinstance(session, dict) or session.get('attachment_body_requested_on_mac') is not False:
        raise AttachmentError('attachment_session_invalid')
    uri = urlsplit(str(session.get('downloadUrl') or ''))
    if uri.scheme != 'https' or uri.hostname not in ORIGINS:
        raise AttachmentError('attachment_download_origin_invalid')
    extension = Path(str(item.get('name') or '')).suffix.lower()
    if not re.fullmatch(r'\.[a-z0-9]{1,12}', extension):
        extension = '.dat'
    declared_size = int(item.get('size') or 0)
    if declared_size < 0 or declared_size > 52428800:
        raise AttachmentError('attachment_size_limit')
    identity = digest(canonical([row['mailbox'], row['message_id'], item['id'], item.get('name'), declared_size]).encode())
    job_id = uuid.uuid4().hex
    request = {'job_id': job_id, 'identity_sha256': identity, 'worker_sha256': value['worker_script_sha256'],
        'download_url': session['downloadUrl'], 'extension': extension, 'declared_size': declared_size,
        'content_cache_request':content_registry.mail_request(handle,extension)}
    # URL is only stdin to SSH, never a saved request or process argument.
    loader = "& ([ScriptBlock]::Create([IO.File]::ReadAllText('" + dispatch + "',[Text.Encoding]::UTF8)))"
    raw = _ssh(value['ssh_host'], loader,
               payload=(json.dumps(request) + '\n').encode(), timeout=1900)
    try:
        result = json.loads(raw.decode('utf-8-sig'))
    except (UnicodeError, ValueError) as exc:
        raise AttachmentError('win11_receipt_invalid') from exc
    result = validate_result(result, job_id=job_id, identity=identity,
        worker_hash=value['worker_script_sha256'], declared_size=declared_size,
        expected_host=value['expected_windows_host'])
    if handle:
        # Revalidate source access at delivery, after a potentially long Windows job.
        try:
            renewed=subprocess.run(command,capture_output=True,timeout=90)
            
            if renewed.returncode != 0: raise AttachmentError('content_source_probe_unavailable')
            fresh=json.loads(renewed.stdout)
            fresh_uri=urlsplit(str(fresh.get('downloadUrl') or ''))
            if fresh.get('attachment_body_requested_on_mac') is not False or fresh_uri.scheme!='https' or fresh_uri.hostname not in ORIGINS:
                raise AttachmentError('content_source_probe_invalid')
            cache=result.get('content_cache') or {}
            profile=cache.get('profile') or {}
            control=request['content_cache_request']
            if (cache.get('mode')!=control['mode'] or cache.get('key')!=cache_key(profile)
                or profile.get('raw_sha256')!=result['source_sha256'] or profile.get('raw_bytes')!=result['source_bytes']
                or profile.get('view_sha256')!=result.get('local_view_sha256')
                or profile.get('view_bytes')!=(result.get('source_binding') or {}).get('view_bytes')
                or profile.get('policy_sha256')!=control['policy_sha256']
                or any(profile.get(k)!=control['context'][k] for k in ('domain','purpose','permission_epoch'))
                or profile.get('format')!=extension or profile.get('scope')!=control['scope']
                or profile.get('redaction')!=control['redaction'] or profile.get('exclusions')!=control['exclusions']):
                raise AttachmentError('content_cache_binding_invalid')
            if cache.get('hit') and (not control['eligible'] or control['mode']!='exact_reuse'
                or result['source_sha256']!=result.get('local_view_sha256')
                or result['extract_status']!='extracted' or cache.get('avoided_parses')!=1):
                raise AttachmentError('content_cache_reuse_invalid')
        except subprocess.TimeoutExpired as exc:
            raise AttachmentError('attachment_source_recheck_timeout') from exc
        except OSError as exc:
            raise AttachmentError('content_source_probe_unavailable') from exc
        except AccessDenied as exc:
            raise AttachmentError('content_source_access_denied') from exc
        except (ValueError,KeyError) as exc:
            raise AttachmentError('content_source_probe_invalid') from exc
        # A changed/missing policy is an authorization failure, not an index outage.
        try:
            if key(content_registry.policy(config))!=key(handle['policy']):raise AttachmentError('content_source_policy_changed')
        except (OSError,ValueError,AccessDenied) as exc:raise AttachmentError('content_source_policy_changed') from exc
        try:content_registry.mail_finish(config,handle,result)
        except AccessDenied as exc:raise AttachmentError('content_source_access_denied') from exc
        except (OSError,sqlite3.Error):result['content_registry_status']='index_unavailable'
    return result


def verify_archived(archive, attachment, file_hashes, envelope, message):
    """Validate source/text receipts offline, without fetching original bytes."""
    if envelope.get('policy_id') == 'mail-staged-except-self-v1':
        if envelope.get('originals_on_m2') != 'images_only': return False
        sender = address(message.get('from'))
        if not sender or digest(sender.encode()) != envelope.get('sender_sha256'): return False
        is_self = digest(sender.encode()) in envelope.get('self_sender_sha256', [])
        if attachment.get('storage') == 'phase_pending':
            aid = attachment.get('attachment_id_hash', '')
            return bool(not is_self and re.fullmatch(r'[0-9a-f]{16}', aid)
                and attachment.get('stored_name') == 'phase-pending/' + aid
                and attachment.get('sha256') == '' and attachment.get('actual_size') is None
                and attachment.get('extract_status') == 'not_attempted'
                and not attachment.get('extracted_text_path') and not attachment.get('receipt_path'))
        envelope = {**envelope, 'policy_id': POLICY_ID, 'originals_on_m2': False}
    if envelope.get('policy_id') != POLICY_ID or envelope.get('originals_on_m2') is not False:
        return False
    sender = address(message.get('from'))
    sender_hash = digest(sender.encode())
    if not sender or sender_hash != envelope.get('sender_sha256'):
        return False
    is_self = sender_hash in envelope.get('self_sender_sha256', [])
    if attachment.get('storage') == 'skipped_self_sender':
        return bool(is_self and attachment.get('extract_status') == 'skipped_self_sender'
            and not attachment.get('sha256') and attachment.get('actual_size') is None
            and not attachment.get('extracted_text_path') and not attachment.get('receipt_path')
            and str(attachment.get('stored_name', '')).startswith('policy-skipped/self-sender/'))
    if attachment.get('storage') == 'video_hold':
        import video_hold
        kind=video_hold.classify(attachment.get('original_name'), attachment.get('declared_content_type',''))
        return bool(not is_self and kind and attachment.get('video_policy') == video_hold.POLICY
            and attachment.get('extract_status') == 'waiting_instruction'
            and attachment.get('sha256') == '' and attachment.get('actual_size') is None
            and not attachment.get('extracted_text_path') and not attachment.get('receipt_path')
            and str(attachment.get('stored_name','')).startswith('policy-held/video/'))
    if is_self or attachment.get('storage') != 'win11':
        return False
    relative = str(attachment.get('receipt_path') or '')
    if not re.fullmatch(r'attachment-receipts/[0-9]+\.json', relative) or relative not in file_hashes:
        return False
    try:
        receipt = json.loads((archive / relative).read_text(encoding='utf-8'))
        text_path = attachment.get('extracted_text_path')
        text = ''
        if text_path:
            if not re.fullmatch(r'attachment-text/[0-9]+\.txt', str(text_path)) or text_path not in file_hashes:
                return False
            text = (archive / text_path).read_text(encoding='utf-8')
        result = validate_result({**receipt, 'text': text}, job_id=receipt['job_id'],
            identity=attachment['identity_sha256'], worker_hash=attachment['worker_sha256'],
            declared_size=int(attachment.get('declared_size') or 0), expected_host='EXAMPLE_USER')
        return (attachment.get('sha256') == result['source_sha256'] and attachment.get('actual_size') == result['source_bytes']
            and attachment.get('extract_status') == result['extract_status'] and attachment.get('risk') == result['risk']
            and attachment.get('stored_name') == 'win11/' + result['job_id'] + '/' + Path(result['remote_path'].replace('\\', '/')).name)
    except (OSError, ValueError, KeyError, AttachmentError):
        return False


def full_read_receipt(config, attachment):
    """Read a verified supplementary receipt without altering a legacy archive."""
    value = policy(config)
    source_hash = str(attachment.get('sha256') or '')
    if not value or not SHA.fullmatch(source_hash):
        return None
    folder = Path(value['worker_script']).parent.parent / 'receipts' / 'attachment-full-read-v1' / source_hash
    receipt_path = folder / 'receipt.json'
    if not receipt_path.is_file():
        return None
    if folder.is_symlink() or receipt_path.is_symlink() or (folder / 'text.txt').is_symlink():
        raise AttachmentError('win11_supplemental_receipt_invalid')
    try:
        receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
        text = (folder / 'text.txt').read_text(encoding='utf-8')
        if receipt.get('schema_version') == 3 and receipt.get('provenance_type') == 'verified_legacy_pdf_ocr':
            evidence = Path(str(receipt.get('evidence_path') or ''))
            permitted = Path(value['worker_script']).parent.parent / 'receipts'
            if not evidence.resolve().is_relative_to(permitted.resolve()) or evidence.is_symlink():
                raise AttachmentError('win11_legacy_receipt_invalid')
            raw = evidence.read_bytes()
            if digest(raw) != receipt.get('evidence_sha256'):
                raise AttachmentError('win11_legacy_receipt_invalid')
            prior = json.loads(raw); result = prior.get('result') or {}; docs = prior.get('documents') or []
            source = prior.get('source_verification') or {}
            if (receipt.get('source_sha256') != source_hash or result.get('source_sha256') != source_hash
                    or source.get('current_archive_sha256') != source_hash
                    or source.get('current_archive_bytes') != attachment.get('actual_size')
                    or receipt.get('source_bytes') != attachment.get('actual_size')
                    or digest(text.encode()) != result.get('text_sha256')
                    or digest(text.encode()) != receipt.get('text_sha256')
                    or len(text.encode('utf-16le')) // 2 != receipt.get('text_chars')
                    or len(docs) != result.get('pdf_count') or not docs
                    or sum(d.get('page_count', 0) for d in docs) != result.get('pages')):
                raise AttachmentError('win11_legacy_receipt_invalid')
            for document in docs:
                pages = document.get('pages') or []
                if (document.get('complete') is not True or document.get('all_pages_rendered') is not True
                        or len(pages) != document.get('page_count')
                        or [p.get('page') for p in pages] != list(range(1, len(pages) + 1))
                        or not all(p.get('rendered') is True for p in pages)):
                    raise AttachmentError('win11_legacy_receipt_invalid')
            return {**receipt, 'text': text, 'extract_status': 'extracted', 'risk': 'ordinary',
                    'coverage': {'complete': True, 'content_truncated': False, 'documents': docs}}
        if (receipt.get('source_sha256') != source_hash
                or receipt.get('source_bytes') != attachment.get('actual_size')
                or receipt.get('schema_version') != 2):
            raise AttachmentError('win11_supplemental_receipt_mismatch')
        return validate_result({**receipt, 'text': text}, job_id=receipt['job_id'],
            identity=receipt['identity_sha256'], worker_hash=receipt['worker_sha256'],
            declared_size=int(attachment.get('declared_size') or 0),
            expected_host=value['expected_windows_host'])
    except (OSError, ValueError, KeyError) as exc:
        raise AttachmentError('win11_supplemental_receipt_invalid') from exc
