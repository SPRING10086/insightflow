param(
    [string]$Dist = "",
    [switch]$SkipSignature,
    [switch]$RequireSignature,
    [switch]$WriteManifest,
    [string]$SmokeResult = ""
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $Dist) { $Dist = Join-Path $root "dist\InsightFlow" }
$Dist = [System.IO.Path]::GetFullPath($Dist)
if (-not $SmokeResult) { $SmokeResult = Join-Path $root "docs\llm-stage5-smoke.json" }

$script:rows = New-Object System.Collections.ArrayList
function Add-Row([string]$name, [string]$status, [string]$detail) {
    [void]$script:rows.Add([pscustomobject]@{ Check = $name; Status = $status; Detail = $detail })
}

function Get-Sha256([string]$path) {
    return (Get-FileHash -Algorithm SHA256 -Path $path).Hash.ToLowerInvariant()
}

Write-Host ("verify_release: " + $Dist)

if (-not (Test-Path $Dist)) {
    Add-Row "dist_exists" "FAIL" "not found: $Dist"
    $script:rows | Format-Table -AutoSize
    exit 1
}

# ---------------------------------------------------------------- required files
$required = @(
    "python\python.exe",
    "python\pythonw.exe",
    "app\src\insightflow\__init__.py",
    "app\requirements.lock.txt",
    "app\config\config.json",
    "启动操作端.vbs",
    "启动操作端(排障).bat"
)
$missing = @()
foreach ($rel in $required) {
    if (-not (Test-Path (Join-Path $Dist $rel))) { $missing += $rel }
}
if ($missing.Count -eq 0) {
    Add-Row "required_files" "PASS" "$($required.Count) files present"
} else {
    Add-Row "required_files" "FAIL" ("missing: " + ($missing -join ", "))
}

# ---------------------------------------------------------------- no secrets
$appDir = Join-Path $Dist "app"
$dotenv = @()
if (Test-Path $appDir) {
    $dotenv = Get-ChildItem -Path $appDir -Recurse -Force -Filter ".env*" -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -ne ".env.example" }
}
if ($dotenv.Count -eq 0) {
    Add-Row "no_dotenv_in_bundle" "PASS" "none"
} else {
    Add-Row "no_dotenv_in_bundle" "FAIL" (($dotenv | ForEach-Object { $_.FullName }) -join ", ")
}

$secretHits = @()
if (Test-Path $appDir) {
    $secretHits = Get-ChildItem -Path $appDir -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object { @(".py", ".json", ".md", ".txt", ".bat", ".vbs", ".ps1", ".ini") -contains $_.Extension.ToLowerInvariant() } |
        Select-String -Pattern "sk-[A-Za-z0-9]{20,}" -List -ErrorAction SilentlyContinue
}
if ($secretHits.Count -eq 0) {
    Add-Row "no_api_key_in_bundle" "PASS" "none"
} else {
    Add-Row "no_api_key_in_bundle" "FAIL" (($secretHits | ForEach-Object { $_.Path }) -join ", ")
}

# ---------------------------------------------------------------- version consistency
$initPath = Join-Path $Dist "app\src\insightflow\__init__.py"
$cfgPath = Join-Path $Dist "app\config\config.json"
$codeVersion = ""
$cfgVersion = ""
if (Test-Path $initPath) {
    $text = Get-Content -Path $initPath -Raw
    if ($text -match '__version__\s*=\s*"([^"]+)"') { $codeVersion = $Matches[1] }
}
if (Test-Path $cfgPath) {
    try { $cfgVersion = (Get-Content -Path $cfgPath -Raw | ConvertFrom-Json).version } catch { $cfgVersion = "" }
}
if ($codeVersion -ne "" -and $codeVersion -eq $cfgVersion) {
    Add-Row "version_consistent" "PASS" $codeVersion
} else {
    Add-Row "version_consistent" "FAIL" "code=$codeVersion config=$cfgVersion"
}

# ---------------------------------------------------------------- manifest
$manifestTargets = @(
    "启动操作端.vbs",
    "启动操作端(排障).bat",
    "python\python.exe",
    "python\pythonw.exe",
    "python\python310.dll",
    "python\vcruntime140.dll",
    "app\config\config.json",
    "app\requirements.lock.txt",
    "app\src\insightflow\__init__.py"
)
$manifestPath = Join-Path $Dist "InsightFlow.sha256"
$existing = @()
foreach ($rel in $manifestTargets) { if (Test-Path (Join-Path $Dist $rel)) { $existing += $rel } }

if ($WriteManifest -or -not (Test-Path $manifestPath)) {
    $lines = @()
    foreach ($rel in $existing) {
        $lines += ((Get-Sha256 (Join-Path $Dist $rel)) + "  " + $rel)
    }
    Set-Content -Path $manifestPath -Value $lines -Encoding UTF8
    Add-Row "manifest" "PASS" ("written: " + $existing.Count + " files")
} else {
    $expected = @{}
    foreach ($line in Get-Content -Path $manifestPath -Encoding UTF8) {
        if ($line -match '^([0-9a-f]{64})\s+(.+)$') { $expected[$Matches[2]] = $Matches[1] }
    }
    $mismatch = @()
    foreach ($rel in $existing) {
        if (-not $expected.ContainsKey($rel)) { $mismatch += "$rel=missing"; continue }
        $actual = Get-Sha256 (Join-Path $Dist $rel)
        if ($actual -ne $expected[$rel]) { $mismatch += "$rel=hash" }
    }
    if ($mismatch.Count -eq 0) {
        Add-Row "manifest" "PASS" ("$($existing.Count) files match")
    } else {
        Add-Row "manifest" "FAIL" ($mismatch -join ", ")
    }
}

# ---------------------------------------------------------------- signatures
if ($SkipSignature) {
    Add-Row "signatures" "WARN" "skipped (-SkipSignature)"
} else {
    $signTargets = @("启动操作端.vbs", "启动操作端(排障).bat", "python\python.exe", "python\pythonw.exe")
    $unsigned = @()
    foreach ($rel in $signTargets) {
        $path = Join-Path $Dist $rel
        if (-not (Test-Path $path)) { continue }
        $sig = Get-AuthenticodeSignature -Path $path
        if ($sig.Status -ne "Valid") { $unsigned += ("$rel=" + $sig.Status) }
    }
    if ($unsigned.Count -eq 0) {
        Add-Row "signatures" "PASS" "all valid"
    } elseif ($RequireSignature) {
        Add-Row "signatures" "FAIL" ($unsigned -join ", ")
    } else {
        Add-Row "signatures" "WARN" ("unsigned: " + ($unsigned -join ", ") + " (use -RequireSignature for release)")
    }
}

# ---------------------------------------------------------------- smoke evidence
if (-not (Test-Path $SmokeResult)) {
    Add-Row "smoke_evidence" "FAIL" "missing: $SmokeResult"
} else {
    try {
        $smoke = Get-Content -Path $SmokeResult -Raw | ConvertFrom-Json
        $failed = [int]$smoke.failed
        $passed = [int]$smoke.passed
        if ($failed -eq 0 -and $passed -ge 17) {
            Add-Row "smoke_evidence" "PASS" ("$passed passed / $failed failed")
        } else {
            Add-Row "smoke_evidence" "FAIL" ("$passed passed / $failed failed")
        }
    } catch {
        Add-Row "smoke_evidence" "FAIL" ("unreadable: " + $_.Exception.Message)
    }
}

# ---------------------------------------------------------------- VC++ runtime
$systemRoot = $env:SystemRoot
if (-not $systemRoot) { $systemRoot = "C:\Windows" }
$vcFiles = @("vcruntime140.dll", "msvcp140.dll")
$vcMissing = @()
foreach ($name in $vcFiles) {
    if (-not (Test-Path (Join-Path $systemRoot ("System32\" + $name)))) { $vcMissing += $name }
}
if ($vcMissing.Count -eq 0) {
    Add-Row "vcpp_runtime" "PASS" "vcruntime140/msvcp140 present"
} else {
    Add-Row "vcpp_runtime" "WARN" ("missing: " + ($vcMissing -join ", ") + " (install VC++ 2015-2022 x64)")
}

# ---------------------------------------------------------------- summary
$script:rows | Format-Table -AutoSize
$failCount = @($script:rows | Where-Object { $_.Status -eq "FAIL" }).Count
$warnCount = @($script:rows | Where-Object { $_.Status -eq "WARN" }).Count
Write-Host ("`n结果：FAIL=" + $failCount + " WARN=" + $warnCount + " / 共 " + $script:rows.Count + " 项")
if ($failCount -gt 0) { exit 1 }
exit 0
