"""Owner-requested media hold. Metadata only; this module never fetches media.

There is deliberately no bypass/approve flag. Ordinary retries and direct entry
points obey the same policy. A later owner instruction requires a scoped change.
"""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import hashlib
import json
import os
import sqlite3
import io
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

POLICY = "all-sources-video-metadata-only-v1"
ROOT = Path(_archive_path('/Users/example/AI/LocalData/BUSINESS-Memory-Bridge.nosync'))
DATABASE = Path(os.environ.get('BUSINESS_VIDEO_HOLD_DB') or str(ROOT / 'state/video-holds.sqlite'))
VIDEO = frozenset('.mp4 .mov .mkv .webm .avi .wmv .flv .m4v .mpeg .mpg .3gp .3g2 .mts .m2ts .ts .vob .ogv .mxf'.split())
NONVIDEO = frozenset(('.7z .a .ai .bash .bat .bz2 .c .cc .cfg .cjs .class .cmd .com .command .conf .cpp .cs .css .csvs .cxx .dll .dmg .docm .dotm .dwg .dxf .dylib .eml .exe .fig .fish .go .gradle .gz .h .hh .hpp .hta .iges .igs .indd .ini .ipynb .jar .java .js .jse .json .jsonl .jsx .kt .kts .less .lib .lnk .lock .lua .map .mbox .mjs .msi .msp .nupkg .o .ost .php .pkg .pl .potm .ppam .pptm .properties .ps1 .psd .psm1 .pst .py .pyw .r .rar .rb .reg .rs .sass .scr .scss .sh .sketch .sldm .sln .so .sql .step .stp .svg .swift .tar .tbz .tbz2 .tgz .toml .ts .tsx .txz .url .vbe .vbs .vue .war .was .wasm .whl .wsf .wsh .xlam .xlsm .xltm .xml .xz .yaml .yml .zip .zsh .txt .md .csv .tsv .json .xml .yaml .yml .log .ini .css .html .htm '
    '.pdf .doc .docx .docm .xls .xlsx .xlsm .ppt .pptx .pptm .rtf .zip .7z .rar .eml .msg '
    '.png .jpg .jpeg .avif .bmp .dib .tif .tiff .gif .webp .ico .emf .wmf .svg '
    '.mp3 .m4a .wav .aac .flac .ogg .opus .amr .silk '
    '.exe .dll .msi .com .scr .bat .cmd .ps1 .vbs .js .hta .lnk .jar '
    '.sqlite .sqlite3 .db .duckdb .parquet .mdb .accdb .dwg .dxf .step .stp .iges .igs .psd .ai .indd .sketch .fig .iso .dmg .pkg').split())

def stamp():
    return datetime.now().astimezone().isoformat(timespec='seconds')

def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()

def classify(name='', mime='', media_type='', *, unknown=True):
    kind = str(media_type or '').lower()
    mime = str(mime or '').split(';', 1)[0].strip().lower()
    try:
        path = urlsplit(str(name or '')).path
    except ValueError:
        path = str(name or '')
    suffix = Path(path.replace('\\', '/')).suffix.lower()
    if kind in {'video', 'short_video', 'videomessage'} or mime.startswith('video/') or suffix in VIDEO:
        return 'video'
    if kind in {'voice', 'audio', 'image', 'emoji', 'text'} or mime.startswith(('audio/', 'image/', 'text/')) or suffix in NONVIDEO:
        return None
    return 'unknown_media' if unknown else None

class HeldMedia(RuntimeError):
    def __init__(self, kind='video', hold_id=None):
        self.kind, self.hold_id = kind, hold_id
        super().__init__('video_waiting_instruction' if kind == 'video' else 'media_type_waiting_confirmation')

def _connect():
    DATABASE.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DATABASE), timeout=10)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('''CREATE TABLE IF NOT EXISTS holds (
      hold_id TEXT PRIMARY KEY,source_id TEXT NOT NULL,object_ref TEXT NOT NULL,
      version TEXT NOT NULL,kind TEXT NOT NULL,name TEXT NOT NULL,parent_label TEXT NOT NULL,
      size_bytes INTEGER,original_state TEXT NOT NULL,first_seen TEXT NOT NULL,
      last_changed TEXT NOT NULL,policy TEXT NOT NULL,UNIQUE(source_id,object_ref,version))''')
    DATABASE.chmod(0o600)
    return c

def register(source, ref, *, name='', parent='', size=None, version=None,
             existing=False, embedded=False, kind='video'):
    # Source locators stay private. Do not persist expiring signed download URLs.
    if '://' in canonical(ref):
        raise ValueError('video_hold_ref_must_be_source_identity')
    ref_text = canonical(ref)
    version = str(version or digest([name, size]))
    ident = digest([source, ref_text, version])[:24]
    state = 'embedded_original' if embedded else 'existing_original' if existing else 'not_downloaded'
    clean = lambda x: ''.join(ch for ch in str(x or '') if ord(ch) >= 32)[:500]
    values = (ident, source, ref_text, version, kind, clean(name), clean(parent),
              int(size) if isinstance(size, (int,float)) and size >= 0 else None, state, stamp(), stamp(), POLICY)
    c = _connect()
    try:
        with c:
            c.execute('''INSERT INTO holds VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(hold_id) DO UPDATE SET name=excluded.name,parent_label=excluded.parent_label,
                size_bytes=excluded.size_bytes,original_state=excluded.original_state,last_changed=excluded.last_changed
                WHERE holds.name!=excluded.name OR holds.parent_label!=excluded.parent_label
                OR holds.size_bytes IS NOT excluded.size_bytes OR holds.original_state!=excluded.original_state''', values)
    finally:
        c.close()
    return ident

def rows():
    if not DATABASE.is_file():
        return []
    with sqlite3.connect(DATABASE.as_uri()+'?mode=ro', uri=True) as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute('SELECT * FROM holds ORDER BY first_seen,hold_id')]

def public_item(row):
    return {k:row.get(k) for k in ('hold_id','source_id','kind','name','parent_label','size_bytes','original_state','first_seen','version')}

def by_id(ident):
    return next((r for r in rows() if r['hold_id'] == ident), None)

def guard(source, ref, name='', mime='', media_type='', **metadata):
    kind = classify(name, mime, media_type)
    if kind:
        ident = register(source, ref, name=name, kind=kind, **metadata)
        raise HeldMedia(kind, ident)

def coverage_summary(receipt):
    nodes = receipt.get('coverage', {}).get('nodes') or []
    videos = [n for n in nodes if n.get('status') in {'deferred_video','deferred_media'} or
              (n.get('error_code') == 'unsupported_content_type' and classify(n.get('name'), unknown=False) == 'video')]
    if not videos:
        return None
    ancestors = {p for n in videos for p in [' / '.join(n['name'].split(' / ')[:i]) for i in range(1,len(n['name'].split(' / ')))]}
    other = [n for n in nodes if n not in videos and n.get('status') not in {'complete','excluded_self_sender'}
             and not (n.get('status') == 'incomplete' and n.get('name') in ancestors)]
    return {'videos':videos, 'non_video_complete':not other, 'other_failures':len(other),
            'text_chars':receipt.get('text_chars'), 'completed_at':receipt.get('completed_at')}

def archived_reading(row):
    """Verify existing receipt + text binding; never rewrite original archives."""
    try:
        root = Path(row['archive_path'])
        raw = (root/'manifest.json').read_bytes()
        if row.get('manifest_sha256') and hashlib.sha256(raw).hexdigest() != row['manifest_sha256']:
            return None
        manifest = json.loads(raw)
        item = next(a for a in manifest['attachments'] if a.get('attachment_id_hash') == row['attachment_id'])
        if item.get('sha256') != row.get('sha256'):
            return None
        hashes = {x['path']:x['sha256'] for x in manifest['files']}
        contents = {}
        for field in ('receipt_path','extracted_text_path'):
            rel = item.get(field)
            if not rel:
                continue
            p = root/rel
            if p.is_symlink() or root.resolve() not in p.resolve().parents:
                return None
            data = p.read_bytes()
            if hashlib.sha256(data).hexdigest() != hashes.get(rel):
                return None
            contents[field] = data
        receipt = json.loads(contents['receipt_path'])
        if receipt.get('source_sha256') != row.get('sha256') or receipt.get('source_bytes') != row.get('actual_size'):
            return None
        text = contents.get('extracted_text_path', b'')
        if hashlib.sha256(text).hexdigest() != receipt.get('text_sha256'):
            return None
        summary = coverage_summary(receipt)
        if summary:
            summary['receipt_verified'] = True
            return summary
    except (KeyError,ValueError,OSError,TypeError,StopIteration):
        return None

def register_attachment(row, summary):
    for n in summary['videos']:
        register('dingtalk_mail', {'message_key':row['message_key'],'attachment_id':row['attachment_id'],'member':n['name']},
            name=n['name'].split(' / ')[-1],parent=row.get('original_name',''),size=n.get('bytes'),
            version=row.get('sha256') or digest(n),embedded=True,
            kind='unknown_media' if n.get('status') == 'deferred_media' else 'video')

def inspect_container(path, source, ref, *, version=None, parent=None):
    """Inventory container metadata. Never read a held member or expand to disk.

    Unlabelled OLE embeddings remain type-unconfirmed here. The attachment reader
    can supply a verified named payload receipt without unpacking the video.
    Existing extraction errors remain the caller's responsibility.
    """
    path = Path(path)
    if path.suffix.lower() not in {'.zip','.xlsx','.xlsm','.docx','.docm','.pptx','.pptm'}:
        return []
    found = []
    budget = [0]
    containers = {'.zip','.xlsx','.xlsm','.docx','.docm','.pptx','.pptm'}
    def inspect(handle, chain, depth):
        if depth > 6:
            return
        with zipfile.ZipFile(handle) as archive:
            entries = archive.infolist()
            if len(entries) > 10000:
                return
            for info in entries:
                if info.is_dir():
                    continue
                member = chain + ' / ' + info.filename
                suffix = Path(info.filename).suffix.lower()
                embedded = '/embeddings/' in '/'+info.filename or '/media/' in '/'+info.filename
                kind = classify(info.filename, unknown=embedded or path.suffix.lower()=='.zip')
                if kind:
                    found.append(register(source, {**ref,'member':member},name=Path(info.filename).name,
                        parent=parent or path.name,size=info.file_size,version=version,
                        existing=True,embedded=True,kind=kind))
                    continue
                if suffix in containers and not info.flag_bits & 1 and info.file_size <= 64*1024*1024:
                    budget[0] += info.file_size
                    if budget[0] <= 128*1024*1024:
                        inspect(io.BytesIO(archive.read(info)),member,depth+1)
    try:
        inspect(path,path.name,0)
    except (OSError,ValueError,RuntimeError,zipfile.BadZipFile):
        pass  # The ordinary reader reports actual corrupt/encrypted containers.
    return found
