
if($env:SYNC_CENTER_ENABLE_ARCHIVED_CODE -ne '1'){throw 'archived_integration_disabled'}
if(-not $env:SYNC_CENTER_HOME){throw 'isolated_root_not_configured'}
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
[Console]::InputEncoding=[Text.Encoding]::UTF8
[Console]::OutputEncoding=[Text.Encoding]::UTF8
$base=Join-Path $env:LOCALAPPDATA 'BusinessMemory\mail-attachments'
$taskName=$null;$registered=$false;$failure='win11_download_failed';$job=$null
try{
 $request=[Console]::ReadLine()|ConvertFrom-Json
 if($request.job_id -notmatch '^[0-9a-f]{32}$' -or $request.identity_sha256 -notmatch '^[0-9a-f]{64}$' -or $request.worker_sha256 -notmatch '^[0-9a-f]{64}$' -or $request.extension -notmatch '^\.[a-z0-9]{1,12}$'){throw 'request_invalid'}
 $allowed=@('.7z','.a','.aac','.accdb','.ai','.amr','.avif','.bash','.bat','.bmp','.bz2','.c','.cc','.cfg','.cjs','.class','.cmd','.com','.command','.conf','.cpp','.cs','.css','.csv','.csvs','.cxx','.db','.dib','.dll','.dmg','.doc','.docm','.docx','.dotm','.duckdb','.dwg','.dxf','.dylib','.emf','.eml','.exe','.fig','.fish','.flac','.gif','.go','.gradle','.gz','.h','.hh','.hpp','.hta','.htm','.html','.ico','.iges','.igs','.indd','.ini','.ipynb','.iso','.jar','.java','.jpeg','.jpg','.js','.jse','.json','.jsonl','.jsx','.kt','.kts','.less','.lib','.lnk','.lock','.log','.lua','.m4a','.map','.mbox','.md','.mdb','.mjs','.mp3','.msg','.msi','.msp','.nupkg','.o','.ogg','.opus','.ost','.parquet','.pdf','.php','.pkg','.pl','.png','.potm','.ppam','.ppt','.pptm','.pptx','.properties','.ps1','.psd','.psm1','.pst','.py','.pyw','.r','.rar','.rb','.reg','.rs','.rtf','.sass','.scr','.scss','.sh','.silk','.sketch','.sldm','.sln','.so','.sql','.sqlite','.sqlite3','.step','.stp','.svg','.swift','.tar','.tbz','.tbz2','.tgz','.tif','.tiff','.toml','.ts','.tsv','.tsx','.txt','.txz','.url','.vbe','.vbs','.vue','.war','.was','.wasm','.wav','.webp','.whl','.wmf','.wsf','.wsh','.xlam','.xls','.xlsm','.xlsx','.xltm','.xml','.xz','.yaml','.yml','.zip','.zsh')
 if($request.extension -notin $allowed){$failure='video_or_unknown_media_waiting_instruction';throw 'metadata_only_policy'}
 $uri=[Uri]$request.download_url
 if($uri.Scheme -ne 'https' -or $uri.Host -notin @('alimail-cn.aliyuncs.com','alimail-personal.aliyuncs.com')){throw 'origin_not_allowed'}
 $worker=Join-Path $base ('workers\'+$request.worker_sha256+'.ps1')
 if((Get-FileHash -LiteralPath $worker -Algorithm SHA256).Hash.ToLowerInvariant() -ne $request.worker_sha256){throw 'worker_identity_mismatch'}
 $job=Join-Path $base ('jobs\'+$request.job_id);New-Item -ItemType Directory -Path $job|Out-Null
 $target=Join-Path $job ('original'+$request.extension)
 Add-Type -AssemblyName System.Net.Http
 $handler=[Net.Http.HttpClientHandler]::new();$handler.AllowAutoRedirect=$false
 $client=[Net.Http.HttpClient]::new($handler);$client.Timeout=[TimeSpan]::FromSeconds(60)
 $response=$client.GetAsync($uri,[Net.Http.HttpCompletionOption]::ResponseHeadersRead).GetAwaiter().GetResult()
 if([int]$response.StatusCode -ne 200){throw 'download_http_failed'}
 if($response.Content.Headers.ContentType.MediaType -like 'video/*'){$failure='video_waiting_instruction';$response.Dispose();$client.Dispose();throw 'metadata_only_policy'}
 $stream=$response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
 $output=[IO.File]::Open($target,[IO.FileMode]::CreateNew);$hasher=[Security.Cryptography.SHA256]::Create();$total=0
 try{
  $buffer=New-Object byte[] 65536
  $cancel=[Threading.CancellationTokenSource]::new(60000)
  while(($n=$stream.ReadAsync($buffer,0,$buffer.Length,$cancel.Token).GetAwaiter().GetResult()) -gt 0){$total+=$n;if($total -gt 52428800){throw 'attachment_size_limit'};[void]$hasher.TransformBlock($buffer,0,$n,$buffer,0);$output.Write($buffer,0,$n)}
  [void]$hasher.TransformFinalBlock([byte[]]@(),0,0);$hash=[BitConverter]::ToString($hasher.Hash).Replace('-','').ToLowerInvariant()
 }finally{$output.Dispose();$stream.Dispose();$response.Dispose();$client.Dispose()}
 if([long]$request.declared_size -gt 0 -and [long]$request.declared_size -ne $total){throw 'source_size_mismatch'}
 $download=@{schema_version=1;job_id=$request.job_id;identity_sha256=$request.identity_sha256;worker_sha256=$request.worker_sha256;source_sha256=$hash;source_bytes=$total;remote_path=$target;source_origin=$uri.GetLeftPart([UriPartial]::Authority);completed_at=[DateTimeOffset]::Now.ToString('o');content_cache_request=$request.content_cache_request}
 [IO.File]::WriteAllText((Join-Path $job 'download.json'),($download|ConvertTo-Json -Depth 12),[Text.UTF8Encoding]::new($false))
 $failure='win11_reader_unavailable'
 $loader="& ([ScriptBlock]::Create([IO.File]::ReadAllText('"+$worker+"',[Text.Encoding]::UTF8))) -JobRoot '"+$job+"'"
 $encoded=[Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($loader))
 $taskName='BusinessMemory-Mail-'+$request.job_id
 $action=New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -STA -EncodedCommand '+$encoded)
 $principal=New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
 $settings=New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Seconds 1800) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
 Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Settings $settings -Description 'One attachment read; no recurring trigger'|Out-Null;$registered=$true
 Start-ScheduledTask -TaskName $taskName
 $deadline=[DateTime]::UtcNow.AddSeconds(1790);$resultPath=Join-Path $job 'result.json'
 while([DateTime]::UtcNow -lt $deadline){
  if(Test-Path -LiteralPath $resultPath){try{$payload=[IO.File]::ReadAllText($resultPath,[Text.Encoding]::UTF8)|ConvertFrom-Json;if($payload.job_id -eq $request.job_id){[Console]::WriteLine(($payload|ConvertTo-Json -Depth 30 -Compress));break}}catch{}}
  Start-Sleep -Seconds 1
 }
 if(-not $payload){$failure='win11_reader_timeout';throw 'worker_incomplete'}
}catch{
 [Console]::WriteLine((@{downloaded=$false;error_code=$failure;error_type=$_.Exception.GetType().FullName;original_received_on_m2=$false}|ConvertTo-Json -Compress))
}finally{
 if($registered){$t=Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue;if($t.State -eq 'Running'){Stop-ScheduledTask -TaskName $taskName};Unregister-ScheduledTask -TaskName $taskName -Confirm:$false}
}
