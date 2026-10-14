[CmdletBinding()]
param(
    [switch]$ValidateOnly,
    [switch]$TestErrorWindow,
    [switch]$ForceClaudeFallback
)
if($env:SYNC_CENTER_ENABLE_ARCHIVED_CODE -ne '1'){throw 'archived_integration_disabled'}
if(-not $env:SYNC_CENTER_HOME){throw 'isolated_root_not_configured'}


Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Utf8NoBom = [Text.UTF8Encoding]::new($false)
$Utf8StrictNoBom = [Text.UTF8Encoding]::new($false, $true)

$ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$PromptPath = Join-Path $PSScriptRoot 'mail_sync_prompt.md'
$SchemaPath = Join-Path $PSScriptRoot 'mail_sync_result.schema.json'
$StateDir = Join-Path $env:LOCALAPPDATA 'BUSINESS-Mail-Mirror\headless'
$LogPath = Join-Path $StateDir 'run.log'
$AlertPath = Join-Path $StateDir 'last-error.txt'
$ErrorDisplayPath = Join-Path $PSScriptRoot 'Show-MailSyncError.ps1'
$DashboardScript = Join-Path $ProjectRoot 'work\sync_dashboard.py'
$DashboardPython = $env:SYNC_CENTER_PYTHON
. (Join-Path $PSScriptRoot 'MailSyncDiagnostics.ps1')
$script:DashboardFailed = $false

# Unified Windows routing; provider credentials are DPAPI protected.
$RouterPython = $env:SYNC_CENTER_PYTHON
$RouterLauncher = Join-Path $env:LOCALAPPDATA 'BusinessMemory\model-router\invoke.py'
$script:MailEngine = 'model-router'

function Write-RunLog {
    param([string]$Status, [string]$Detail)
    $safeDetail = ($Detail -replace '[\r\n]+', ' ' -replace 'https://[^\s]+', '[URL redacted]')
    if ($safeDetail.Length -gt 500) { $safeDetail = $safeDetail.Substring(0, 500) }
    $line = "{0}`t{1}`t{2}" -f ([DateTimeOffset]::Now.ToString('o')), $Status, $safeDetail
    Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8
}

function Invoke-DashboardUpdate {
    param(
        [string]$Phase,
        [string]$RunId,
        [string]$ResultPath = '',
        [int]$FinalExitCode = 0,
        [string]$FailureCode = ''
    )
    $savedErrorAction = $ErrorActionPreference
    Write-MailDiagnostic ('dashboard.' + $Phase) 'started'
    try {
        if (-not (Test-Path -LiteralPath $DashboardPython -PathType Leaf)) {
            throw 'DASHBOARD_PYTHON_UNAVAILABLE'
        }
        if (-not (Test-Path -LiteralPath $DashboardScript -PathType Leaf)) {
            throw 'DASHBOARD_SCRIPT_UNAVAILABLE'
        }
        $dashboardArgs = @('-X', 'utf8', $DashboardScript, $Phase, '--run-id', $RunId)
        if ($Phase -eq 'finish') {
            $dashboardArgs += @('--result-path', $ResultPath, '--exit-code', [string]$FinalExitCode)
            if (-not [string]::IsNullOrWhiteSpace($FailureCode)) {
                $dashboardArgs += @('--error-code', $FailureCode)
            }
        }
        $ErrorActionPreference = 'Continue'
        & $DashboardPython @dashboardArgs 2>&1 | ForEach-Object {
            if ([string]$_ -match '^error: DIAGNOSTIC_WRITE_FAILED$') {
                $script:MailDiagnosticFailed = $true
                Write-MailDiagnostic ('dashboard.' + $Phase) 'failed' @{code='DIAGNOSTIC_WRITE_FAILED'}
            }
        }
        $dashboardResult = $LASTEXITCODE
        $ErrorActionPreference = $savedErrorAction
        if ($dashboardResult -ne 0) {
            $script:DashboardFailed = $true
            Write-MailDiagnostic ('dashboard.' + $Phase) 'failed' @{code='DASHBOARD_UPDATE_FAILED';exit_code=$dashboardResult}
            try { Write-RunLog 'dashboard-error' ("phase={0}; exit={1}; run_id={2}" -f $Phase, $dashboardResult, $RunId) } catch {}
        }
        else {
            Write-MailDiagnostic ('dashboard.' + $Phase) 'completed' @{exit_code=0}
            if ($Phase -eq 'finish') {
                if ($script:DashboardFailed) {
                    Write-MailDiagnostic 'dashboard.recovery' 'completed' @{code='DASHBOARD_RECOVERED';exit_code=0}
                }
                $script:DashboardFailed = $false
            }
        }
    }
    catch {
        $script:DashboardFailed = $true
        Write-DiagnosticException ('dashboard.' + $Phase) $_ 'DASHBOARD_UPDATE_EXCEPTION'
        try { Write-RunLog 'dashboard-error' ("phase={0}; run_id={1}; update unavailable" -f $Phase, $RunId) } catch {}
    }
    finally { $ErrorActionPreference = $savedErrorAction }
}

function Complete-DashboardUpdate {
    param([string]$RunId, [string]$ResultPath, [int]$ExitCode, [string]$Code)
    # The final publication can recover a failed begin update. Do not archive
    # that earlier failure as the mail result before recovery is attempted.
    $mailStatus = Get-MailFinalStatus $ExitCode $Code -IgnoreDashboardFailure
    Invoke-DashboardUpdate 'finish' $RunId $ResultPath $mailStatus.exit_code $mailStatus.code
    return (Get-MailFinalStatus $ExitCode $Code)
}

function Resolve-CodexPath {
    $command = Get-Command codex.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($command) { return $command.Source }

    $binRoot = Join-Path $env:LOCALAPPDATA 'OpenAI\Codex\bin'
    $candidate = Get-ChildItem -LiteralPath $binRoot -Filter codex.exe -Recurse -File -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if (-not $candidate) { throw 'CODEX_CLI_NOT_FOUND' }
    return $candidate.FullName
}

function Get-CodexFailureCode {
    param([string]$Line)

    # Inspect native output only in memory. Never persist raw diagnostic text,
    # JSON events, tool output, email content, or authentication material.
    $text = $Line.Trim()
    if ($text.StartsWith('{')) {
        try {
            $event = $text | ConvertFrom-Json
            if ($event.PSObject.Properties.Name -notcontains 'type') { return '' }
            if ([string]$event.type -notin @('error', 'turn.failed')) { return '' }
            $messages = @()
            foreach ($field in @('code', 'message')) {
                if ($event.PSObject.Properties.Name -contains $field) { $messages += [string]$event.$field }
            }
            if ($event.PSObject.Properties.Name -contains 'error' -and $null -ne $event.error) {
                if ($event.error -is [string]) { $messages += $event.error }
                else {
                    foreach ($field in @('code', 'message')) {
                        if ($event.error.PSObject.Properties.Name -contains $field) { $messages += [string]$event.error.$field }
                    }
                }
            }
            $text = $messages -join ' '
        }
        catch { return '' }
    }
    elseif ($text -notmatch '^(?i)(error:|fatal:|\S+\s+ERROR\s)') { return '' }

    # Only these fixed codes leave this function. Unknown errors remain unknown.
    switch -Regex ($text) {
        'DIAGNOSTIC_WRITE_FAILED' { return 'DIAGNOSTIC_WRITE_FAILED' }
        '(?i)usage.?limit|insufficient_quota|quota.{0,30}(exceeded|exhausted)|rate_limit_exceeded|rate limit' { return 'CODEX_USAGE_LIMIT' }
        '(?i)refresh_token_reused|invalid_api_key|unauthorized|authentication.{0,30}(failed|required)|token.{0,15}expired|401 Unauthorized' { return 'CODEX_AUTH_FAILED' }
        '(?i)model.{0,80}(not supported|unsupported|not found|not available)|unsupported.{0,40}(model|reasoning)|model_not_found' { return 'CODEX_MODEL_UNAVAILABLE' }
        '(?i)(untrusted|trust).{0,60}hook|hook.{0,60}(untrusted|trust)' { return 'CODEX_HOOK_TRUST_REQUIRED' }
        '(?i)(failed|invalid|error).{0,40}(config|schema)|error loading configuration' { return 'CODEX_CONFIG_INVALID' }
        '(?i)connection (refused|reset)|timed out|timeout|error sending request|stream disconnected|unexpected status (429|5[0-9]{2})' { return 'CODEX_NETWORK_ERROR' }
        default { return '' }
    }
}

function Get-CodexFailureSummary {
    param([string]$Code, [int]$ExitCode)
    switch ($Code) {
        'CODEX_USAGE_LIMIT' { return 'Codex 用量或请求速率达到限制；队列已保留。' }
        'CODEX_AUTH_FAILED' { return 'Codex 身份验证失败；队列已保留。' }
        'CODEX_MODEL_UNAVAILABLE' { return 'Codex 原定模型或思考设置不可用；队列已保留。' }
        'CODEX_HOOK_TRUST_REQUIRED' { return 'Codex 启动钩子需要信任检查；队列已保留。' }
        'CODEX_CONFIG_INVALID' { return 'Codex 配置或输出结构无效；队列已保留。' }
        'CODEX_NETWORK_ERROR' { return 'Codex 请求连接失败或中断；队列已保留。' }
        default { return "CLI 退出码 $ExitCode；未识别具体原因，队列已保留。" }
    }
}

function Get-SafeAlertText {
    param([string]$Stage, [string]$Summary, [string]$ReportPath)

    $safeSummary = ($Summary -replace '[\r\n]+', ' ' -replace 'https://[^\s]+', '[URL redacted]')
    if ($safeSummary.Length -gt 400) { $safeSummary = $safeSummary.Substring(0, 400) }
    $safeReport = if ([string]::IsNullOrWhiteSpace($ReportPath)) { '未生成' } else { $ReportPath }
    $diagFolder = if ($script:MailDiagnosticFile) { Split-Path -Parent $script:MailDiagnosticFile } else { '未初始化' }
    return "[公司邮箱同步异常]`n时间：$([DateTimeOffset]::Now.ToString('yyyy-MM-dd HH:mm:ss zzz'))`n阶段：$Stage`n错误：$safeSummary`n报告：$safeReport`n运行编号：$script:MailDiagnosticRunId`n诊断记录：$diagFolder"
}

function Update-IncidentState {
    param([string]$Code)
    $helper = Join-Path (Split-Path -Parent $PSScriptRoot) 'mail_incident.py'
    $incidentState = Join-Path $StateDir 'incident-state.json'
    $signalPath = 'C:\Users\Example\Desktop\解密文件\业务记忆 Win11\同步技术状态.json'
    $decision = & $DashboardPython -X utf8 $helper --state $incidentState --export $signalPath --code $Code
    if ($LASTEXITCODE -ne 0) { throw 'INCIDENT_STATE_UPDATE_FAILED' }
    return ($decision | ConvertFrom-Json)
}

function Show-ErrorWindow {
    param([string]$Stage, [string]$Summary, [string]$ReportPath, [string]$IncidentCode='runtime_failure')

    $decision = Update-IncidentState $IncidentCode
    if (-not $decision.notify) { return }
    if (-not (Test-Path -LiteralPath $ErrorDisplayPath -PathType Leaf)) {
        throw 'ERROR_DISPLAY_SCRIPT_MISSING'
    }

    $alertText = Get-SafeAlertText $Stage $Summary $ReportPath
    Set-Content -LiteralPath $AlertPath -Value $alertText -Encoding UTF8

    $powerShellPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    if (-not (Test-Path -LiteralPath $powerShellPath -PathType Leaf)) {
        throw 'WINDOWS_POWERSHELL_NOT_FOUND'
    }

    $arguments = @(
        '-NoLogo',
        '-NoProfile',
        '-ExecutionPolicy', 'Bypass',
        '-File', ('"{0}"' -f $ErrorDisplayPath),
        '-AlertPath', ('"{0}"' -f $AlertPath)
    )
    Start-Process -FilePath $powerShellPath -ArgumentList $arguments -WindowStyle Normal | Out-Null
}

New-Item -ItemType Directory -Path $StateDir -Force | Out-Null

if ($ValidateOnly) {
    if (-not (Test-Path -LiteralPath $PromptPath -PathType Leaf)) { throw 'PROMPT_FILE_MISSING' }
    if (-not (Test-Path -LiteralPath $SchemaPath -PathType Leaf)) { throw 'SCHEMA_FILE_MISSING' }
    if (-not (Test-Path -LiteralPath $RouterLauncher -PathType Leaf)) { throw 'MODEL_ROUTER_MISSING' }
    [pscustomobject]@{ valid=$true; engine='model-router'; project_root=$ProjectRoot; prompt_path=$PromptPath; schema_path=$SchemaPath } | ConvertTo-Json
    exit 0
}
if ($ForceClaudeFallback) { throw 'USE_ISOLATED_MODEL_ROUTER_PROBE' }

if ($TestErrorWindow) {
    Show-ErrorWindow '窗口测试' '这是脱敏错误窗口测试，不代表邮箱同步失败。' ''
    Write-RunLog 'error-window-test' 'success'
    exit 0
}

$createdNew = $false
$mutex = [Threading.Mutex]::new($true, 'Local\Example CorpMailMemoryHeadless', [ref]$createdNew)
if (-not $createdNew) {
    Write-RunLog 'skipped' 'previous run still active'
    $mutex.Dispose()
    exit 0
}

$resultPath = Join-Path $StateDir ('result-{0}-{1}.json' -f (Get-Date -Format 'yyyyMMdd-HHmmss'), $PID)
$dashboardRunId = [Guid]::NewGuid().ToString('D')
$previousDashboardRunId = [Environment]::GetEnvironmentVariable('BUSINESS_MAIL_SYNC_RUN_ID', 'Process')
$env:BUSINESS_MAIL_SYNC_RUN_ID = $dashboardRunId
$dashboardExitCode = 3
$dashboardErrorCode = 'RUN_INTERRUPTED'
$result = $null
$validatedReportPath = ''
$resultPending = 0
$resultBusiness = 0
$runnerStage = 'runner.start'


try {
    Initialize-MailDiagnostic $StateDir $dashboardRunId @(
        $PSCommandPath, (Join-Path $PSScriptRoot 'MailSyncDiagnostics.ps1'), $PromptPath, $SchemaPath,
        (Join-Path $ProjectRoot 'work\mail_mirror.py'), (Join-Path $ProjectRoot 'work\mail_diagnostics.py'),
        (Join-Path $ProjectRoot 'work\mail_attachment_reuse.py'),
        (Join-Path $ProjectRoot 'work\mail_legacy_xls.py'), (Join-Path $ProjectRoot 'work\mail_review_paging.py'), $DashboardScript
    )
    Invoke-DashboardUpdate 'begin' $dashboardRunId
    $runnerStage = 'runner.resolve'
    Write-MailDiagnostic $runnerStage 'started'
    if (-not (Test-Path -LiteralPath $RouterLauncher -PathType Leaf)) { throw 'MODEL_ROUTER_MISSING' }
    Write-MailDiagnostic 'runner.resolve' 'completed' @{file='invoke.py';sha256=(Get-FileHash -LiteralPath $RouterLauncher -Algorithm SHA256).Hash.ToLowerInvariant()}
    $runnerStage = 'agent.router'
    Write-MailDiagnostic $runnerStage 'started'
    $savedErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $routeOutput = & $RouterPython -X utf8 $RouterLauncher mail_route_runner --prompt $PromptPath --schema $SchemaPath --result $resultPath --workdir $ProjectRoot --run-id $dashboardRunId 2>$null
        $routeExit = $LASTEXITCODE
    } finally { $ErrorActionPreference = $savedErrorAction }
    if ($routeExit -ne 0) {
        $dashboardExitCode = $routeExit
        $dashboardErrorCode = if ($routeExit -eq 2) {'MODEL_OR_PROCESSING_FAILED'} else {'COLLECTION_OR_CONTROL_FAILED'}
        Write-MailDiagnostic $runnerStage 'failed' @{code='MODEL_ROUTE_FAILED';exit_code=$routeExit}
        Write-RunLog 'error' ("engine=model-router; queue retained; run_id={0}" -f $dashboardRunId)
        Show-ErrorWindow '同步处理' '本轮处理未完成；队列、逐项回执和具体失败阶段已保留。' ''
        exit $routeExit
    }
    $routeMeta = ($routeOutput -join "`n") | ConvertFrom-Json
    $script:MailEngine = [string]$routeMeta.provider
    Write-MailDiagnostic $runnerStage 'completed' @{model=[string]$routeMeta.model;exit_code=0}

    $runnerStage = 'runner.result'
    Write-MailDiagnostic $runnerStage 'started'
    if (-not (Test-Path -LiteralPath $resultPath -PathType Leaf)) {
        throw 'STRUCTURED_RESULT_MISSING'
    }

    # Codex writes the structured result as UTF-8 without a BOM. An implicit
    # Get-Content decode under Windows PowerShell 5.1 corrupts Chinese text and
    # can consume JSON delimiters, producing false error alerts.
    $resultJson = [IO.File]::ReadAllText($resultPath, $Utf8StrictNoBom)
    $result = $resultJson | ConvertFrom-Json
    if (-not [string]::IsNullOrWhiteSpace([string]$result.report_path)) {
        $reportRoot = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'BUSINESS-Mail-Mirror\mirror\reports')).TrimEnd('\') + '\'
        $resultReport = [IO.Path]::GetFullPath([string]$result.report_path)
        if (-not $resultReport.StartsWith($reportRoot, [StringComparison]::OrdinalIgnoreCase) -or [IO.Path]::GetFileName($resultReport) -cnotmatch '^sync_\d{8}-\d{6}\.json$') { throw 'STRUCTURED_REPORT_PATH_INVALID' }
        $result.report_path = $resultReport
        if (Test-Path -LiteralPath $resultReport -PathType Leaf) { $validatedReportPath = $resultReport }
    }
    $resultPending = [int]$result.pending_remaining
    $resultBusiness = [int]$result.business_processed
    Write-MailDiagnostic $runnerStage $(if ($result.status -in @('success','attention')) {'completed'} else {'failed'}) @{code=$(if ($result.status -in @('success','attention')) {'RESULT_VALID'} else {'STRUCTURED_RESULT_ERROR'});pending=$resultPending;business_processed=$resultBusiness}
    if ($result.status -eq 'error') {
        Show-ErrorWindow '邮箱同步任务' '正文或附件处理失败；具体步骤、错误码和异常位置见本次诊断记录。' ([string]$result.report_path) $(if ($result.model_invoked) {'model_failure'} else {'collection_failure'})
        $dashboardExitCode = 2
        $dashboardErrorCode = 'STRUCTURED_RESULT_ERROR'
        Write-RunLog 'error' ("engine={0}; pending={1}; report={2}; run_id={3}" -f $script:MailEngine, $result.pending_remaining, $result.report_path, $dashboardRunId)
        exit 2
    }

    if ($result.status -notin @('success', 'attention')) { throw 'STRUCTURED_RESULT_STATUS_INVALID' }
    if (Test-Path -LiteralPath $AlertPath -PathType Leaf) {
        Remove-Item -LiteralPath $AlertPath -Force
    }
    $dashboardExitCode = 0
    $dashboardErrorCode = ''
    Write-RunLog ([string]$result.status) ("engine={0}; business={1}; ignored={2}; pending={3}; report={4}; run_id={5}" -f $script:MailEngine, $result.business_processed, $result.prefilter_ignored, $result.pending_remaining, $result.report_path, $dashboardRunId)
    exit 0
}
catch {
    Write-DiagnosticException $runnerStage $_ 'RUNNER_EXCEPTION'
    $errorCode = 'RUNNER_EXCEPTION'
    if ($_.Exception.Message -cmatch '^[A-Z][A-Z0-9_]{2,80}$') { $errorCode = $_.Exception.Message }
    try {
        Show-ErrorWindow '后台运行器' $errorCode '' $(if ($errorCode -match 'COMMIT') {'commit_conflict'} else {'runtime_failure'})
    }
    catch {
        Write-DiagnosticException 'alert.display' $_ 'ERROR_WINDOW_FAILED'
        Write-RunLog 'error-window-failed' 'ERROR_WINDOW_FAILED'
    }
    $dashboardExitCode = 3
    $dashboardErrorCode = 'RUNNER_EXCEPTION'
    Write-RunLog 'error' ("{0}; run_id={1}" -f $errorCode, $dashboardRunId)
    exit 3
}
finally {
    $finalStatus = Complete-DashboardUpdate $dashboardRunId $resultPath $dashboardExitCode $dashboardErrorCode
    $finalExitCode = $finalStatus.exit_code
    if ($script:DashboardFailed -and $dashboardExitCode -eq 0 -and $finalExitCode -eq 4) {
        Write-RunLog 'error' ("code=DASHBOARD_UPDATE_FAILED; run_id={0}" -f $dashboardRunId)
        try { Show-ErrorWindow '看板更新' '邮件处理已有记录，最终看板更新仍失败；异常位置和重试过程已保留。' $validatedReportPath 'dashboard_failure' } catch { Write-DiagnosticException 'alert.display' $_ 'ERROR_WINDOW_FAILED' }
    }
    if ($finalExitCode -eq 0) {
        try {
            if ($null -ne $result -and $result.status -eq 'attention') {
                Show-ErrorWindow '有待处理' '邮件收取正常；未读内容保留待处理，重复提醒间隔为24小时。' $validatedReportPath 'attachment_blocked'
            } else {
                Show-ErrorWindow '运行恢复' '此前同步故障已恢复，本轮收取和处理正常。' $validatedReportPath 'normal'
            }
        } catch {
            Write-DiagnosticException 'alert.state' $_ 'INCIDENT_STATE_UPDATE_FAILED'
            $finalExitCode = 6
        }
    }
    Complete-MailDiagnostic $finalExitCode $finalStatus.code $resultPending $resultBusiness
    if ($script:MailDiagnosticFailed) {
        $finalExitCode = 5
        try { Write-RunLog 'error' ("code=DIAGNOSTIC_INCOMPLETE; run_id={0}" -f $dashboardRunId); Show-ErrorWindow '诊断记录' '故障记录读取、解析或写入不完整；本轮不标为全部正常，具体错误码见诊断记录。' '' } catch {}
    }
    [Environment]::SetEnvironmentVariable('BUSINESS_MAIL_SYNC_RUN_ID', $previousDashboardRunId, 'Process')
    if (Test-Path -LiteralPath $resultPath) { Remove-Item -LiteralPath $resultPath -Force -ErrorAction SilentlyContinue }
    $mutex.ReleaseMutex()
    $mutex.Dispose()
    if ($finalExitCode -ne $dashboardExitCode) { exit $finalExitCode }
}
