# YUI v2 Windows launcher (spec 32).
#
# Startup order:
#   network -> Ollama -> Ollama health -> DB integrity/schema -> recovery
#   -> world catch-up -> scheduler restore -> readiness -> Discord connect
#
# The application itself performs everything from "DB integrity" onwards; this
# script is responsible only for the two things outside it — waiting for the
# machine to be ready, and refusing to start against a database that must not
# be there (spec 32: the live DB is never in a cloud-synced folder, and the
# production writer is exactly one Windows host).

# `rebuild-reset` is deliberately absent from this list. The launcher runs
# migrate and a backup before whatever it was asked for, and a command that
# replaces the database must not be reachable through the everyday path
# (rebuild spec 3.4). Run it directly:
#   .venv\Scripts\python.exe -m app.main rebuild-reset --confirm ERASE_YUI_STATE
param(
    [ValidateSet("run", "migrate", "status", "backup", "diagnose", "repair", "capabilities", "rebuild-status")]
    [string]$Command = "run",
    [string]$OllamaUrl = "http://127.0.0.1:11434",
    [int]$OllamaTimeoutSeconds = 120
)

$ErrorActionPreference = "Stop"
Set-Location -Path (Split-Path -Parent $PSScriptRoot)

$python = Join-Path (Get-Location) ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "virtualenv not found at $python; run: python -m venv .venv; .venv\Scripts\pip install -e .[dev]"
}

# --- spec 32: the live database must not sit in a sync folder ---------------
$dataDir = if ($env:YUI_DATA_DIR) { $env:YUI_DATA_DIR } else { Join-Path (Get-Location) "data" }
foreach ($marker in @("OneDrive", "Dropbox", "iCloud", "Google Drive")) {
    if ($dataDir -like "*$marker*") {
        throw "the live database is inside $marker ($dataDir). Spec 32 forbids this: it will corrupt the database."
    }
}

# --- wait for the network ----------------------------------------------------
$deadline = (Get-Date).AddSeconds(60)
while (-not (Test-Connection -ComputerName 127.0.0.1 -Count 1 -Quiet)) {
    if ((Get-Date) -gt $deadline) { throw "network did not come up within 60s" }
    Start-Sleep -Seconds 2
}

# --- wait for Ollama ---------------------------------------------------------
# A missing model degrades the runtime rather than corrupting it (spec 28.3),
# so a timeout here is a warning, not a failure.
$deadline = (Get-Date).AddSeconds($OllamaTimeoutSeconds)
$ollamaReady = $false
while (-not $ollamaReady) {
    try {
        Invoke-WebRequest -Uri "$OllamaUrl/api/tags" -UseBasicParsing -TimeoutSec 5 | Out-Null
        $ollamaReady = $true
    } catch {
        if ((Get-Date) -gt $deadline) {
            Write-Warning "Ollama not reachable at $OllamaUrl; starting degraded (spec 28.3)"
            break
        }
        Start-Sleep -Seconds 3
    }
}

# --- schema, then the application -------------------------------------------
& $python -m app.main migrate
if ($LASTEXITCODE -ne 0) { throw "migration failed with exit code $LASTEXITCODE" }

# An automatic backup before every start, verified by the application itself.
& $python -m app.main backup --reason "pre-start"
if ($LASTEXITCODE -ne 0) { Write-Warning "pre-start backup could not be verified" }

# Patch spec 23.3: say so when the Genesis under this database never produced a
# person. A warning, not a refusal — the real Discord history in there is the
# reason a repair is careful rather than automatic.
if ($Command -eq "run") {
    & $python -m app.main diagnose | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "this database booted from a Genesis that did not run; see docs/REPAIR.md"
    }
}

& $python -m app.main $Command
exit $LASTEXITCODE
