[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$stamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
$acceptanceRoot = Join-Path $projectRoot "data/private/deployment-acceptance/$stamp"
$databasePath = Join-Path $acceptanceRoot "acceptance.sqlite3"
$queuePath = Join-Path $acceptanceRoot "actions.json"
$auditPath = Join-Path $acceptanceRoot "audit/actions.jsonl"
$backupDir = Join-Path $acceptanceRoot "backups"
$restoreDatabasePath = Join-Path $acceptanceRoot "restore-drill.sqlite3"
$serviceConfigPath = Join-Path $acceptanceRoot "service.yaml"

function Invoke-InboxJson {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    $output = & uv run inbox-agent @Arguments 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) {
        throw "InboxPilot command failed with exit code ${LASTEXITCODE}: $output"
    }
    try {
        return $output | ConvertFrom-Json
    }
    catch {
        throw "InboxPilot returned invalid JSON: $output"
    }
}

function Assert-Equal {
    param(
        [Parameter(Mandatory = $true)]$Actual,
        [Parameter(Mandatory = $true)]$Expected,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if ($Actual -ne $Expected) {
        throw "$Label expected '$Expected' but received '$Actual'"
    }
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv is required. Run scripts/Install-InboxPilot.ps1 first."
}

Push-Location $projectRoot
try {
    $null = New-Item -ItemType Directory -Path $acceptanceRoot -Force
    @"
schema_version: "1.0"
service_name: inbox-pilot-acceptance
interval_minutes: 15
max_backoff_minutes: 60
run_immediately: true
lock_path: data/private/deployment-acceptance/$stamp/service.lock
notifications:
  enabled: false
  desktop_enabled: false
  daily_summary_enabled: false
observability:
  enabled: true
  log_path: data/private/deployment-acceptance/$stamp/logs/inbox-pilot.jsonl
  llm_pricing: []
workflow:
  dataset_path: data/samples/sample_emails.json
  database_path: data/private/deployment-acceptance/$stamp/acceptance.sqlite3
  action_queue_path: data/private/deployment-acceptance/$stamp/actions.json
  audit_log_path: data/private/deployment-acceptance/$stamp/audit/actions.jsonl
  policy_path: config/rules.yaml
  llm_config_path: null
  llm_routing_path: config/llm_routing.yaml
  llm_fusion_path: config/llm_fusion.yaml
  sync_outlook: false
  graph_config_path: config/graph.local.yaml
"@ | Set-Content -LiteralPath $serviceConfigPath -Encoding UTF8

    Write-Host "Running an isolated offline workflow twice..."
    $workflowArguments = @(
        "workflow", "run",
        "--dataset", "data/samples/sample_emails.json",
        "--database", $databasePath,
        "--queue", $queuePath,
        "--audit-log", $auditPath,
        "--format", "json"
    )
    $first = Invoke-InboxJson -Arguments $workflowArguments
    $second = Invoke-InboxJson -Arguments $workflowArguments
    Assert-Equal $first.eligible_messages 50 "first run eligible_messages"
    Assert-Equal $first.actions_added 50 "first run actions_added"
    Assert-Equal $first.graph_write_request_count 0 "first run Graph writes"
    Assert-Equal $second.eligible_messages 0 "second run eligible_messages"
    Assert-Equal $second.skipped_current 50 "second run skipped_current"
    Assert-Equal $second.actions_generated 0 "second run actions_generated"
    Assert-Equal $second.graph_write_request_count 0 "second run Graph writes"

    Write-Host "Checking statistics and diagnostics..."
    $stats = Invoke-InboxJson -Arguments @(
        "stats", "--database", $databasePath, "--hours", "8760", "--format", "json"
    )
    Assert-Equal $stats.workflow_runs 2 "workflow run count"
    Assert-Equal $stats.action_backlog 50 "action backlog"
    $doctorOutput = & uv run inbox-agent doctor `
        --database $databasePath `
        --service-config $serviceConfigPath `
        --backup-dir $backupDir `
        --format json 2>&1 | Out-String
    if ($LASTEXITCODE -notin @(0, 2)) {
        throw "Doctor failed with exit code ${LASTEXITCODE}: $doctorOutput"
    }
    $doctor = $doctorOutput | ConvertFrom-Json
    if (-not $doctor.healthy) {
        throw "Doctor reported an unhealthy deployment: $doctorOutput"
    }

    Write-Host "Running a backup and controlled restore drill..."
    $backup = Invoke-InboxJson -Arguments @(
        "backup", "--database", $databasePath, "--output-dir", $backupDir, "--format", "json"
    )
    Assert-Equal $backup.integrity_check "ok" "backup integrity"
    Assert-Equal $backup.revision "0005_observability" "backup revision"
    Copy-Item -LiteralPath $databasePath -Destination $restoreDatabasePath
    $restore = Invoke-InboxJson -Arguments @(
        "restore", $backup.backup_path,
        "--database", $restoreDatabasePath,
        "--service-config", $serviceConfigPath,
        "--backup-dir", (Join-Path $acceptanceRoot "pre-restore"),
        "--confirm", "--format", "json"
    )
    Assert-Equal $restore.integrity_check "ok" "restore integrity"
    Assert-Equal $restore.revision "0005_observability" "restore revision"
    if (-not $restore.pre_restore_backup) {
        throw "Restore did not create a pre-restore backup."
    }

    Write-Host "Deployment acceptance PASSED."
    Write-Host "Artifacts: $acceptanceRoot"
}
finally {
    Pop-Location
}

