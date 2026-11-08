# InsightFlow launcher (PowerShell)
# Usage: .\run.ps1 run --date 2026-11-07 --force
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$env:PYTHONPATH = Join-Path $root "src"
$env:PYTHONIOENCODING = "utf-8"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
Push-Location $root
try {
    & python -m insightflow @args
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
