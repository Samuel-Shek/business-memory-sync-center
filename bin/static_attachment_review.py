"""Static attachment inspection. Never launches attachments or follows links."""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import hashlib
import html
import io
import json
import re
import stat
import zipfile
from pathlib import Path, PurePosixPath

MAX_BYTES = 50 * 1024 * 1024
ACTIVE = {'.exe', '.dll', '.js', '.vbs', '.cmd', '.bat', '.ps1', '.sh', '.app', '.com', '.scr'}
TEXT = {'.txt', '.csv', '.md', '.json', '.xml', '.html', '.htm'}
OFFICE = {'.docx', '.xlsx', '.pptx', '.docm', '.xlsm', '.pptm'}


def inspect(data, suffix, limit=50000, depth=0):
    result = {"text": "", "status": "unrecognized_format", "reasons": [], "active_content_executed": False}
    def stop(reason):
        result.update(status=reason, reasons=[reason])
        return result
    if len(data) > MAX_BYTES:
        return stop('static_size_limit')
    if suffix in ACTIVE or data.startswith((b'MZ', b'\x7fELF', b'#!')):
        return stop('active_file_not_executed')
    if data.startswith(b'%PDF-'):
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data), strict=True)
            if reader.is_encrypted:
                return stop('encrypted_no_password')
            if len(reader.pages) > 300:
                return stop('static_page_limit')
            parts = []
            used = 0
            for page in reader.pages:
                text = page.extract_text() or ''
                parts.append(text[:max(0, limit-used)])
                used += len(parts[-1])
                if used >= limit:
                    result['reasons'].append('text_limit')
                    break
            result.update(text='\n'.join(parts)[:limit], status='extracted_static' if used else 'no_text_static')
            return result
        except ImportError:
            return stop('pdf_parser_unavailable')
        except Exception:
            return stop('pdf_invalid_or_unsupported')
    if zipfile.is_zipfile(io.BytesIO(data)):
        if depth > 2:
            return stop('archive_depth_limit')
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                entries = archive.infolist()
                if len(entries) > 500:
                    return stop('archive_entry_limit')
                total = sum(i.file_size for i in entries)
                compressed = sum(max(1, i.compress_size) for i in entries)
                if total > MAX_BYTES or total / max(1, compressed) > 200:
                    return stop('archive_expansion_risk')
                for item in entries:
                    name = item.filename.replace('\\', '/')
                    if name.startswith('/') or '..' in PurePosixPath(name).parts or re.match(r'^[A-Za-z]:', name) or stat.S_ISLNK(item.external_attr >> 16):
                        return stop('archive_path_risk')
                    if item.flag_bits & 1:
                        return stop('encrypted_no_password')
                parts = []
                office = suffix in OFFICE or '[Content_Types].xml' in archive.namelist()
                for item in entries:
                    name = item.filename.replace('\\', '/')
                    lower = name.lower()
                    ext = PurePosixPath(lower).suffix
                    if item.is_dir():
                        continue
                    if ext in ACTIVE or lower.endswith('vbaproject.bin') or '/embeddings/' in lower:
                        result['reasons'].append('embedded_content_not_opened')
                        continue
                    if sum(map(len, parts)) >= limit:
                        result['reasons'].append('text_limit')
                        break
                    if office:
                        if ext != '.xml' or not name.startswith(('word/', 'xl/sharedStrings', 'xl/worksheets/', 'ppt/slides/', 'ppt/notesSlides/')):
                            continue
                        raw = archive.read(item)
                        if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
                            result['reasons'].append('xml_declaration_not_parsed')
                            continue
                        text = html.unescape(re.sub(r'<[^>]+>', ' ', raw.decode('utf-8', errors='replace')))
                        parts.append(re.sub(r'\s+', ' ', text).strip())
                    elif ext in TEXT | OFFICE | {'.pdf', '.zip'}:
                        child = inspect(archive.read(item), ext, max(1, limit-sum(map(len, parts))), depth+1)
                        if child['text']:
                            parts.append(name+'\n'+child['text'])
                        result['reasons'].extend(child['reasons'])
                    else:
                        result['reasons'].append('nontext_member_preserved')
                result['reasons'] = sorted(set(result['reasons']))
                result.update(text='\n'.join(parts)[:limit], status='partial_static' if result['reasons'] else ('extracted_static' if parts else 'no_text_static'))
                return result
        except (OSError, ValueError, RuntimeError, zipfile.BadZipFile):
            return stop('archive_invalid')
    if suffix in TEXT:
        try:
            text = data.decode('utf-8-sig')
        except UnicodeError:
            return stop('text_encoding_unrecognized')
        if suffix in {'.html', '.htm'}:
            text = re.sub(r'<script\b[^>]*>.*?</script>', '', text, flags=re.I|re.S)
            text = html.unescape(re.sub(r'<[^>]+>', ' ', text))
        result.update(text=text[:limit], status='extracted_static')
        return result
    if data.startswith(b'\x88\x7d\x1c'):
        return stop('protected_or_unknown_container')
    if data.startswith(bytes.fromhex('d0cf11e0a1b11ae1')):
        return stop('legacy_or_encrypted_office_requires_parser')
    return result


def review_pending(config):
    import email_sync as mail
    if mail.win11_attachments.policy(config):
        return {"reviewed": 0, "items": [], "originals_preserved": True,
                "status": "use_win11_attachment_workflow",
                "message": "附件已统一由 Win11 处理；此旧 Mac 静态解析入口不再读取附件。"}
    paths = mail.runtime_paths(config)
    connection = mail.connect_database(config)
    rows = connection.execute("SELECT a.*,m.archive_path FROM attachments a JOIN messages m USING(message_key) WHERE a.status='needs_review' ORDER BY a.message_key,a.attachment_id").fetchall()
    results = []
    version = mail.sha256_file(Path(__file__))
    try:
        for row in rows:
            archive = Path(row['archive_path'])
            source = mail.resolved_under(archive, row['stored_name'])
            if not source or source.is_symlink() or not source.is_file() or mail.sha256_file(source) != row['sha256']:
                raise mail.EmailSyncError('static_source_mismatch', 'attachment does not match its immutable receipt')
            target = paths['state_root'] / 'attachment-static' / row['message_key'] / row['attachment_id'] / row['sha256'] / version
            receipt_path = target / 'receipt.json'
            if receipt_path.is_file():
                receipt = mail.read_json(receipt_path, {})
            else:
                result = inspect(source.read_bytes(), source.suffix.lower(), int(config['email_sync']['max_attachment_text_chars']))
                text = result.pop('text')
                receipt = {"schema_version": 1, "source_sha256": row['sha256'], "parser_sha256": version,
                           "reviewed_at": mail.now_iso(), "text_sha256": mail.sha256_text(text),
                           "text_chars": len(text), **result}
                mail.ensure_private_dir(target)
                mail.atomic_write_text(target/'text.txt', text)
                mail.atomic_write_text(receipt_path, json.dumps(receipt, ensure_ascii=False, indent=2)+'\n')
            complete = receipt['status'] in {'extracted_static', 'no_text_static'}
            connection.execute('UPDATE attachments SET extract_status=?,status=?,last_error_code=? WHERE message_key=? AND attachment_id=? AND sha256=?',
                               (receipt['status'], 'verified' if complete else 'needs_review',
                                None if complete else receipt['status'], row['message_key'], row['attachment_id'], row['sha256']))
            results.append({"attachment_id": row['attachment_id'], "message_key": row['message_key'],
                            "receipt": str(receipt_path), "status": receipt['status'],
                            "text_chars": receipt['text_chars'], "reasons": receipt['reasons']})
        connection.commit()
    finally:
        connection.close()
    return {"reviewed": len(results), "items": results, "originals_preserved": True}
