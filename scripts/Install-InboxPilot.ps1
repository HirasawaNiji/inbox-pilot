[CmdletBinding()]
param(
    [switch]$SkipDependencySync
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($env:OS -ne "Windows_NT") {
    throw "This installer targets Windows PowerShell. Use the manual deployment guide elsewhere."
}

$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))

function Invoke-Uv {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    & uv @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "uv command failed with exit code ${LASTEXITCODE}: uv $($Arguments -join ' ')"
    }
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv is required. Install it from https://docs.astral.sh/uv/ and rerun this script."
}

Push-Location $projectRoot
try {
    if (-not (Test-Path -LiteralPath "pyproject.toml" -PathType Leaf)) {
        throw "pyproject.toml is missing from $projectRoot"
    }

    if (-not $SkipDependencySync) {
        Write-Host "Synchronizing the locked Python environment..."
        Invoke-Uv -Arguments @("sync", "--locked")
    }

    $privateTemplates = @(
        @{ Source = ".env.example"; Target = ".env" },
        @{ Source = "config/service.personal.example.yaml"; Target = "config/service.local.yaml" },
        @{ Source = "config/graph.example.yaml"; Target = "config/graph.local.yaml" },
        @{ Source = "config/graph_write.example.yaml"; Target = "config/graph_write.local.yaml" },
        @{ Source = "config/llm_provider.example.yaml"; Target = "config/llm_provider.local.yaml" }
    )
    foreach ($template in $privateTemplates) {
        if (-not (Test-Path -LiteralPath $template.Target)) {
            Copy-Item -LiteralPath $template.Source -Destination $template.Target
            Write-Host "Created private template: $($template.Target)"
        }
        else {
            Write-Host "Preserved existing private file: $($template.Target)"
        }
    }

    $privateDirectories = @(
        "data/private",
        "data/private/audit",
        "data/private/backups",
        "data/private/logs",
        "data/private/summaries"
    )
    foreach ($directory in $privateDirectories) {
        $null = New-Item -ItemType Directory -Path $directory -Force
    }

    Write-Host "Initializing or upgrading the private SQLite database..."
    Invoke-Uv -Arguments @(
        "run", "inbox-agent", "db", "init",
        "--database", "data/private/inbox_pilot.sqlite3",
        "--format", "json"
    )

    Write-Host "Running read-only diagnostics..."
    & uv run inbox-agent doctor `
        --database data/private/inbox_pilot.sqlite3 `
        --service-config config/service.local.yaml `
        --backup-dir data/private/backups `
        --format json
    if ($LASTEXITCODE -notin @(0, 2)) {
        throw "InboxPilot diagnostics failed with exit code $LASTEXITCODE"
    }

    Write-Host ""
    Write-Host "InboxPilot installation is ready."
    Write-Host "1. Edit config/graph.local.yaml and complete read-only Outlook login."
    Write-Host "2. Keep LLM and writeback disabled until separately configured."
    Write-Host "3. Start with: .\scripts\Start-InboxPilot.ps1 -Mode Web"
}
finally {
    Pop-Location
}
