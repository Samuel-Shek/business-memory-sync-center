"""Bounded, static local read evidence. Transient probe files never enter memory."""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import io
import json
from pathlib import Path
import re
import subprocess
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from handoff_core import digest, Rejected

PLAIN={'.txt','.csv','.md','.json','.xml','.log'}
OOXML={'.docx','.xlsx','.pptx'}
RISK={'.exe','.dll','.bat','.cmd','.ps1','.vbs','.js','.scr','.lnk','.app','.sh','.msi'}


def extension(ref):
    name=ref.get('name')
    if not isinstance(name,str) or not name: raise Rejected('resource_name_missing')
    ext=Path(name).suffix.lower()
    if not re.fullmatch(r'\.[a-z0-9]{1,12}',ext): raise Rejected('extension_unknown')
    return ext


def classify_bytes(data,ext):
    if ext in RISK: return 'risk_file'
    if ext not in PLAIN|OOXML: return 'unsupported_format'
    if len(data)>32*1024*1024: return 'unsupported_format'
    if ext in PLAIN:
        try:
            text=data.decode('utf-8-sig')
            if '\x00' in text: return 'local_parse_failed'
            if ext=='.json': json.loads(text)
            if ext=='.xml':
                if '<!DOCTYPE' in text or '<!ENTITY' in text: return 'risk_file'
                ET.fromstring(text)
            return 'readable_local'
        except (UnicodeError,ValueError,ET.ParseError): return 'local_parse_failed'
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            infos=z.infolist()
            if len(infos)>5000 or sum(i.file_size for i in infos)>64*1024*1024: return 'risk_file'
            if any(i.flag_bits&1 for i in infos): return 'encrypted'
            names=z.namelist()
            if any('vbaproject' in n.lower() or n.startswith('/') or '..' in Path(n).parts for n in names): return 'risk_file'
            if '[Content_Types].xml' not in names: return 'local_parse_failed'
            # Local lightweight parser does not claim coverage of embedded
            # media/objects, tracked changes, notes or binary workbook parts.
            if any('/media/' in n or '/embeddings/' in n or n.endswith('.bin') for n in names): return 'unsupported_format'
            for info in infos:
                if info.filename.endswith('.xml'):
                    data=z.read(info)
                    if b'<!DOCTYPE' in data or b'<!ENTITY' in data: return 'risk_file'
                    ET.fromstring(data)
            if z.testzip() is not None: return 'local_parse_failed'
            return 'readable_local'
    except (zipfile.BadZipFile,UnicodeError,ValueError,ET.ParseError,RuntimeError):
        return 'local_parse_failed'


def probe(source,request,root):
    ref=request['resource']; ext=extension(ref)
    if ext in RISK: return {'reason':'risk_file','extension':ext}
    if ext not in PLAIN|OOXML: return {'reason':'unsupported_format','extension':ext}
    # Plain/OOXML supported in principle: route only after an actual static
    # read fails or coverage proves unsupported. No file handler is launched.
    root=Path(root); root.mkdir(parents=True,exist_ok=True,mode=0o700)
    with tempfile.TemporaryDirectory(prefix='probe-',dir=root) as tmp:
        output=Path(tmp)/('attachment'+ext)
        args=[source.binary,'--profile',source.profile,'chat','+messages-resource-download',
            '--type',ref['type'],'--resource-id',ref['resourceId'],'--output','./'+output.name,'--format','json']
        if ref['type']=='mediaId': args+=['--message-id',request['message'],'--open-conversation-id',request['conversation']]
        try: result=subprocess.run(args,cwd=tmp,capture_output=True,timeout=180)
        except (OSError,subprocess.TimeoutExpired): raise Rejected('network_failed')
        if result.returncode:
            # Do not misclassify retrieval/permission failures as encryption.
            raise Rejected('source_unavailable')
        if not output.is_file() or output.is_symlink(): raise Rejected('source_download_incomplete')
        if output.stat().st_size>32*1024*1024:
            return {'reason':'unsupported_format','extension':ext,'probe_bytes':output.stat().st_size}
        data=output.read_bytes()
        return {'reason':classify_bytes(data,ext),'extension':ext,'probe_sha256':digest(data),'probe_bytes':len(data)}
