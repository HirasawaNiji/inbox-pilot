[CmdletBinding()]
param(
    [ValidateSet("Web", "Service", "RunOnce", "Doctor")]
    [string]$Mode = "Web",

    [ValidateRange(1, 65535)]
    [int]$Port = 8765,

    [switch]$Background,

    [string]$EnvironmentFile = ".env"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))

function Import-EnvironmentFile {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return
    }
    foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) {
            continue
        }
        $separator = $trimmed.IndexOf("=")
        if ($separator -lt 1) {
            throw "Invalid environment entry in ${Path}: expected NAME=VALUE"
        }
        $name = $trimmed.Substring(0, $separator).Trim()
        $value = $trimmed.Substring($separator + 1).Trim()
        if ($name -notmatch "^[A-Za-z_][A-Za-z0-9_]*$") {
            throw "Invalid environment variable name in ${Path}: $name"
        }
        if ($value.Length -ge 2) {
            $quoted = ($value.StartsWith('"') -and $value.EndsWith('"')) -or
                ($value.StartsWith("'") -and $value.EndsWith("'"))
            if ($quoted) {
                $value = $value.Substring(1, $value.Length - 2)
            }
        }
        if ($value -and $null -eq [Environment]::GetEnvironmentVariable($name, "Process")) {
            [Environment]::SetEnvironmentVariable($name, $value, "Process")
        }
    }
}

function Assert-LastExitCode {
    param([Parameter(Mandatory = $true)][string]$Operation)

    if ($LASTEXITCODE -ne 0) {
        throw "$Operation failed with exit code $LASTEXITCODE"
    }
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv is required. Run scripts/Install-InboxPilot.ps1 first."
}
if ($Background -and $Mode -ne "Web") {
    throw "-Background is supported only with -Mode Web."
}

Push-Location $projectRoot
try {
    Import-EnvironmentFile -Path (Join-Path $projectRoot $EnvironmentFile)
    if (-not (Test-Path -LiteralPath "config/service.local.yaml" -PathType Leaf)) {
        throw "config/service.local.yaml is missing. Run scripts/Install-InboxPilot.ps1 first."
    }

    switch ($Mode) {
        "Doctor" {
            & uv run inbox-agent doctor `
                --database data/private/inbox_pilot.sqlite3 `
                --service-config config/service.local.yaml `
                --backup-dir data/private/backups
            if ($LASTEXITCODE -notin @(0, 2)) {
                throw "InboxPilot diagnostics failed with exit code $LASTEXITCODE"
            }
        }
        "RunOnce" {
            & uv run inbox-agent service run-once --config config/service.local.yaml
            Assert-LastExitCode -Operation "InboxPilot single workflow run"
        }
        "Service" {
            & uv run inbox-agent service start --config config/service.local.yaml
            Assert-LastExitCode -Operation "InboxPilot scheduler"
        }
        "Web" {
            $listener = Get-NetTCPConnection `
                -LocalPort $Port `
                -State Listen `
                -ErrorAction SilentlyContinue
            if ($listener) {
                throw "Port $Port is already in use. Inspect the listener before starting InboxPilot."
            }
            if (-not $Background) {
                & uv run inbox-agent web start --port $Port
                Assert-LastExitCode -Operation "InboxPilot Web"
                break
            }

            $logDirectory = Join-Path $projectRoot "data/private/logs"
            $null = New-Item -ItemType Directory -Path $logDirectory -Force
            $stdoutPath = Join-Path $logDirectory "web.stdout.log"
            $stderrPath = Join-Path $logDirectory "web.stderr.log"
            $uvPath = (Get-Command uv).Source
            $arguments = @("run", "inbox-agent", "web", "start", "--port", "$Port")
            $process = Start-Process `
                -FilePath $uvPath `
                -ArgumentList $arguments `
                -WorkingDirectory $projectRoot `
                -WindowStyle Hidden `
                -RedirectStandardOutput $stdoutPath `
                -RedirectStandardError $stderrPath `
                -PassThru
            [IO.File]::WriteAllText(
                (Join-Path $projectRoot "data/private/web-launcher.pid"),
                "$($process.Id)`n"
            )

            $healthy = $false
            for ($attempt = 0; $attempt -lt 40; $attempt++) {
                if ($process.HasExited) {
                    throw "InboxPilot Web exited early. Inspect $stderrPath"
                }
                try {
                    $response = Invoke-WebRequest `
                        -Uri "http://127.0.0.1:$Port/api/v1/health" `
                        -UseBasicParsing `
                        -TimeoutSec 2
                    if ($response.StatusCode -eq 200) {
                        $healthy = $true
                        break
                    }
                }
                catch {
                    Start-Sleep -Milliseconds 500
                }
            }
            if (-not $healthy) {
                throw "InboxPilot Web did not become healthy. Inspect $stderrPath"
            }
            Write-Host "InboxPilot Web is running in the background (launcher PID $($process.Id))."
            Write-Host "Console: http://127.0.0.1:$Port/console"
            Write-Host "Use the console's Full exit action to stop the process and release the port."
        }
    }
}
finally {
    Pop-Location
}
