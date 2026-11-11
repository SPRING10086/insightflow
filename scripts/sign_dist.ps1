# InsightFlow dist 签名 / 公钥导出 / 哈希清单（任务 4 本机部分）
# 用法：powershell -ExecutionPolicy Bypass -File scripts\sign_dist.ps1 [-Dist dist\InsightFlow]
param(
    [string]$Dist = "dist\InsightFlow"
)

$ErrorActionPreference = "Stop"
$root = Resolve-Path $Dist
$cert = Get-ChildItem Cert:\CurrentUser\My |
    Where-Object { $_.Subject -like '*InsightFlow Test Code Signing*' -and $_.HasPrivateKey } |
    Select-Object -First 1
if (-not $cert) {
    throw "未找到 CN=InsightFlow Test Code Signing（CurrentUser\My，含私钥）"
}

$targets = @(
    "启动操作端.vbs",
    "python\python.exe",
    "python\pythonw.exe",
    "python\python3.dll",
    "python\python310.dll",
    "python\vcruntime140.dll",
    "python\vcruntime140_1.dll"
)

$rows = @()
foreach ($rel in $targets) {
    $path = Join-Path $root $rel
    if (-not (Test-Path -LiteralPath $path)) {
        $rows += [pscustomobject]@{ File = $rel; Before = "MISSING"; After = "SKIPPED" }
        continue
    }
    $before = (Get-AuthenticodeSignature -FilePath $path).Status
    $sig = Set-AuthenticodeSignature -FilePath $path -Certificate $cert -HashAlgorithm SHA256
    $after = (Get-AuthenticodeSignature -FilePath $path).Status
    $rows += [pscustomobject]@{ File = $rel; Before = $before; After = $after }
}

$cerPath = Join-Path $root "InsightFlow-TestRoot.cer"
Export-Certificate -Cert $cert -FilePath $cerPath -Type CERT | Out-Null

# 本机信任（CurrentUser 根，无需管理员）；VM 需装到 LocalMachine 根。
$thumb = $cert.Thumbprint
$installed = Get-ChildItem Cert:\CurrentUser\Root | Where-Object { $_.Thumbprint -eq $thumb }
if (-not $installed) {
    Import-Certificate -FilePath $cerPath -CertStoreLocation Cert:\CurrentUser\Root | Out-Null
}

& (Join-Path $PSScriptRoot "verify_release.ps1") -Dist $root -WriteManifest -SkipSmoke | Out-Host

$rows | Format-Table -AutoSize | Out-String | Write-Host
Write-Host "CERT_THUMBPRINT=$thumb"
Write-Host "CERT_FILE=$cerPath"
