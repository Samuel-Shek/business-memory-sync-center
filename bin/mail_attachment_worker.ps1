param([Parameter(Mandatory=$true)][string]$JobRoot)
if($env:SYNC_CENTER_ENABLE_ARCHIVED_CODE -ne '1'){throw 'archived_integration_disabled'}
if(-not $env:SYNC_CENTER_HOME){throw 'isolated_root_not_configured'}

$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
$python=$env:SYNC_CENTER_PYTHON
if(-not $python){throw 'python_not_configured'}
if(-not(Test-Path -LiteralPath $python)){throw 'windows_full_reader_runtime_unavailable'}
foreach($support in @('archive_guard.py','archive_paths.py')) { Copy-Item -LiteralPath (Join-Path $PSScriptRoot $support) -Destination (Join-Path $JobRoot $support) }
$source=Join-Path $JobRoot 'full-reader.py'
[IO.File]::WriteAllBytes($source,[IO.File]::ReadAllBytes((Join-Path $PSScriptRoot 'mail-worker-source/full-reader.py')))
if((Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash.ToLowerInvariant() -ne '41c1ced1687d5546786576b69a01e87527b043755905a0ab263f5a274ed635a8'){throw 'reader_binding_mismatch'}
$module=Join-Path $JobRoot 'content_identity.py'
[IO.File]::WriteAllBytes($module,[IO.File]::ReadAllBytes((Join-Path $PSScriptRoot 'mail-worker-source/content_identity.py')))
if((Get-FileHash -LiteralPath $module -Algorithm SHA256).Hash.ToLowerInvariant() -ne '17fb9337da30a90775182a9dbc0693a194c583e6e773768154305fb1a642899f'){throw 'reader_binding_mismatch'}
$policy=Join-Path $JobRoot 'video_hold.py'
[IO.File]::WriteAllBytes($policy,[IO.File]::ReadAllBytes((Join-Path $PSScriptRoot 'mail-worker-source/video_hold.py')))
if((Get-FileHash -LiteralPath $policy -Algorithm SHA256).Hash.ToLowerInvariant() -ne '86deda9fb83dd8a08da3d8d2322eb993779f39b7598aaf4f9111bbc82bb34cc7'){throw 'video_policy_binding_mismatch'}
& $python $source --job $JobRoot
if($LASTEXITCODE -ne 0){throw 'full_reader_failed'}
$r=Get-Content -LiteralPath (Join-Path $JobRoot 'full-result.json') -Raw -Encoding UTF8|ConvertFrom-Json
if($r.reader_sha256 -ne '41c1ced1687d5546786576b69a01e87527b043755905a0ab263f5a274ed635a8'){throw 'reader_receipt_mismatch'}
$temp=Join-Path $JobRoot 'result.json.tmp'
[IO.File]::WriteAllText($temp,($r|ConvertTo-Json -Depth 30),[Text.UTF8Encoding]::new($false))
Move-Item -LiteralPath $temp -Destination (Join-Path $JobRoot 'result.json') -Force
