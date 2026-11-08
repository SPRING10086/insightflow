# Entry point used by Windows Task Scheduler.
# Keeps output in a dedicated file; structured run records are written by the app.
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = Join-Path $root "src"
$env:PYTHONIOENCODING = "utf-8"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
$log = Join-Path $root "logs\scheduler_console.log"
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $log) | Out-Null
Push-Location $root
try {
    # Keep the full Python output for troubleshooting; decoding is explicit
    # UTF-8 so Chinese text is not mangled by the OEM code page.
    $output = & python -m insightflow run --trigger scheduled 2>&1
    $code = $LASTEXITCODE
    $header = "===== {0} exit={1} =====" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $code
    $header | Out-File -FilePath $log -Append -Encoding utf8
    if ($null -ne $output) {
        $output | Out-File -FilePath $log -Append -Encoding utf8
    }

    # Also append a compact machine-readable summary.
    $line = "{0} exit={1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $code
    $latest = Join-Path $root "logs\latest_run.json"
    if (Test-Path $latest) {
        try {
            $run = Get-Content $latest -Raw -Encoding UTF8 | ConvertFrom-Json
            $line = "{0} exit={1} status={2} target={3} report={4} reason={5}" -f `
                (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $code, $run.status, `
                $run.target_date, $run.report_path, $run.failure_reason
        }
        catch {
            # Keep the basic line if the JSON is unavailable/corrupt.
        }
    }
    $line | Out-File -FilePath $log -Append -Encoding utf8

    # 0 success, 2 no target data, 3 idempotent/concurrent skip are normal
    # scheduled outcomes.  Only a genuine failure (1) should mark the Windows
    # task as failed.
    if ($code -eq 1) { exit 1 } else { exit 0 }
}
finally {
    Pop-Location
}
