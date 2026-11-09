<#
.SYNOPSIS
    Build a portable "unzip and run" bundle: bundled Python + app + no-console launcher.

.DESCRIPTION
    NFR-7: the operator must not install Python.  The bundle copies the verified
    interpreter (locked pandas/numpy already installed) next to the application,
    writes a .vbs launcher that starts pythonw.exe with no console window, and a
    .bat fallback that keeps the console for troubleshooting.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\build_portable.ps1
    powershell -ExecutionPolicy Bypass -File scripts\build_portable.ps1 -OutDir D:\dist\InsightFlow
#>
param(
    [string]$OutDir = "",
    [switch]$SkipSmoke
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $OutDir) { $OutDir = Join-Path $root "dist\InsightFlow" }
$out = [System.IO.Path]::GetFullPath($OutDir)

function Step([string]$text) { Write-Host ("== " + $text) }

# --- 1. Preconditions -------------------------------------------------------
Step "check interpreter"
$python = (Get-Command python -ErrorAction Stop).Source
$version = & $python -c "import sys; print('.'.join(map(str, sys.version_info[:3])))"
$pandas = & $python -c "import pandas, numpy; print(pandas.__version__ + ' / ' + numpy.__version__)"
Write-Host ("   python {0}  ({1})" -f $version, $python)
Write-Host ("   pandas / numpy {0}" -f $pandas)
if (-not $version.StartsWith("3.10")) { throw "expected Python 3.10.x, got $version" }
$pythonHome = Split-Path -Parent $python

# --- 2. Clean target --------------------------------------------------------
Step "clean $out"
& $python -c "import shutil, sys; shutil.rmtree(sys.argv[1], ignore_errors=True)" $out
New-Item -ItemType Directory -Force -Path $out | Out-Null

# --- 3. Copy the interpreter ------------------------------------------------
Step "copy interpreter (portable)"
$excludeDirs = @("__pycache__", "Scripts", "Doc", "include", "libs", "Lib\test", "Lib\tests", "Lib\idlelib", "Lib\lib2to3", "Lib\ensurepip", "Lib\site-packages\pip", "Lib\site-packages\setuptools", "Lib\site-packages\pkg_resources")
$roboArgs = @($pythonHome, (Join-Path $out "python"), "/E", "/NFL", "/NDL", "/NJH", "/NJS", "/NP", "/R:1", "/W:1")
foreach ($dir in $excludeDirs) { $roboArgs += @("/XD", $dir) }
& robocopy @roboArgs | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy python failed: $LASTEXITCODE" }

# --- 4. Copy the application ------------------------------------------------
Step "copy application"
$appDir = Join-Path $out "app"
New-Item -ItemType Directory -Force -Path $appDir | Out-Null
$appExclude = @(".git", "logs", "state", "output", "dist", "tests", "__pycache__", ".github", "acceptance_data", ".agents", "report", "clean_data", "raw_data", "dax", "verification")
$appArgs = @($root, $appDir, "/E", "/NFL", "/NDL", "/NJH", "/NJS", "/NP", "/R:1", "/W:1", "/XF", "*.pyc", "*.pyo")
foreach ($dir in $appExclude) { $appArgs += @("/XD", $dir) }
& robocopy @appArgs | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy app failed: $LASTEXITCODE" }

$configPath = Join-Path $appDir "config\config.json"
if (-not (Test-Path $configPath)) {
    Copy-Item (Join-Path $appDir "config\config.example.json") $configPath
}

# --- 5. Launchers -----------------------------------------------------------
Step "write launchers"
$vbs = @'
Option Explicit
' InsightFlow launcher: no console window.
Dim sh, fso, base, env
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
base = fso.GetParentFolderName(WScript.ScriptFullName)
Set env = sh.Environment("PROCESS")
env("PYTHONPATH") = base & "\app\src"
env("PYTHONIOENCODING") = "utf-8"
env("INSIGHTFLOW_HOME") = base & "\app"
sh.CurrentDirectory = base & "\app"
sh.Run """" & base & "\python\pythonw.exe"" -m insightflow.gui", 0, False
'@
[System.IO.File]::WriteAllText((Join-Path $out "启动操作端.vbs"), $vbs, [System.Text.Encoding]::ASCII)

$bat = @'
@echo off
rem Troubleshooting launcher: keeps a console so errors stay visible.
setlocal
chcp 65001 >nul
set "BASE=%~dp0"
set "PYTHONPATH=%BASE%app\src"
set "PYTHONIOENCODING=utf-8"
set "INSIGHTFLOW_HOME=%BASE%app"
pushd "%BASE%app"
"%BASE%python\python.exe" -m insightflow.gui
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" (
  echo.
  echo InsightFlow GUI exited with code %CODE%.
  echo See app\logs\insightflow.log for details.
  pause
)
popd
exit /b %CODE%
'@
[System.IO.File]::WriteAllText((Join-Path $out "启动操作端(排障).bat"), $bat, [System.Text.Encoding]::ASCII)

$readme = @"
InsightFlow 操作端（免安装版）

启动：双击「启动操作端.vbs」（不会出现黑色命令行窗口）。
排障：若窗口没有出现，双击「启动操作端(排障).bat」，按提示查看 app\logs\insightflow.log。

目录说明
  python\   随包携带的 Python 3.10（已装好 pandas/numpy/openpyxl/xlrd 锁定版本，无需另外安装）
  app\      程序与配置（config\config.json 是实际配置，examples\ 里是演示数据）
  启动操作端.vbs        正常启动
  启动操作端(排障).bat  带控制台启动，用于排查启动失败

使用顺序
  1) 点「放入待分析数据」，把 Excel / CSV 放进数据源目录（或点「选择文件…」）；
  2) 点「刷新清单」，确认文件出现；
  3) 确认目标日期与日期口径，点「开始分析」；
  4) 完成后在「结果」区打开清洗数据或报告；质量分与告警说明哪些数字要谨慎引用。

数据全部在本机处理，不联网、不上传。
"@
[System.IO.File]::WriteAllText((Join-Path $out "使用说明.txt"), $readme, (New-Object System.Text.UTF8Encoding($false)))

# --- 6. Smoke test ----------------------------------------------------------
if (-not $SkipSmoke) {
    Step "smoke test (bundled interpreter, clean env)"
    $env:PYTHONPATH = Join-Path $appDir "src"
    $env:INSIGHTFLOW_HOME = $appDir
    Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue
    Push-Location $appDir
    try {
        $result = & (Join-Path $out "python\python.exe") -c "import insightflow, insightflow.gui.app, pandas, tkinter; print('imports ok', pandas.__version__)" 2>&1
        Write-Host ("   " + ($result -join " "))
        if ($LASTEXITCODE -ne 0) { throw "bundled interpreter import failed" }
    }
    finally {
        Pop-Location
    }
}

# --- 7. Summary -------------------------------------------------------------
$sizeMb = (Get-ChildItem $out -Recurse -File | Measure-Object Length -Sum).Sum / 1MB
Step ("done: {0}  ({1:N0} MB)" -f $out, $sizeMb)
