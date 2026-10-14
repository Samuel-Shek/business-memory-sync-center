#!/usr/bin/env python3
"""Build the pinned Windows worker from reviewable Python sources."""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

from pathlib import Path
import base64, hashlib, json
ROOT=Path(__file__).resolve().parents[1]
worker="""param([Parameter(Mandatory=$true)][string]$JobRoot)
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
$python='C:/Users/example/AppData/Local/BusinessMemory/mail-attachments/runtime/Scripts/python.exe'
if(-not(Test-Path -LiteralPath $python)){throw 'windows_full_reader_runtime_unavailable'}
"""
for name,filename in [('source','mail_attachment_reader.py'),('module','content_identity.py')]:
    data=(ROOT/'bin'/filename).read_bytes();h=hashlib.sha256(data).hexdigest()
    worker+=f"${name}=Join-Path $JobRoot '{'full-reader.py' if name=='source' else filename}'\n"
    worker+=f"[IO.File]::WriteAllBytes(${name},[Convert]::FromBase64String('{base64.b64encode(data).decode()}'))\n"
    worker+=f"if((Get-FileHash -LiteralPath ${name} -Algorithm SHA256).Hash.ToLowerInvariant() -ne '{h}'){{throw 'reader_binding_mismatch'}}\n"
    if name=='source':reader_hash=h
worker+="""& $python $source --job $JobRoot
if($LASTEXITCODE -ne 0){throw 'full_reader_failed'}
$r=Get-Content -LiteralPath (Join-Path $JobRoot 'full-result.json') -Raw -Encoding UTF8|ConvertFrom-Json
"""
worker+=f"if($r.reader_sha256 -ne '{reader_hash}'){{throw 'reader_receipt_mismatch'}}\n"
worker+="""$temp=Join-Path $JobRoot 'result.json.tmp'
[IO.File]::WriteAllText($temp,($r|ConvertTo-Json -Depth 30),[Text.UTF8Encoding]::new($false))
Move-Item -LiteralPath $temp -Destination (Join-Path $JobRoot 'result.json') -Force
"""
(ROOT/'bin/mail_attachment_worker.ps1').write_text(worker)
c=json.loads((ROOT/'config.json').read_text());v=c['email_sync']['attachment_processing']
for name in ['worker_script','dispatch_script']:
    local=ROOT/'bin'/Path(v[name]).name
    v[name]=str(local);v[name+'_sha256']=hashlib.sha256(local.read_bytes()).hexdigest()
c['cross_source_policy']=str(ROOT/'policies/cross_source_dedup.v1.json')
(ROOT/'config.json').write_text(json.dumps(c,ensure_ascii=False,indent=2)+'\n')
print('Pinned reader and dispatcher built.')
