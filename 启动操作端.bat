@echo off
rem InsightFlow desktop launcher.
rem Keep this file ASCII-only: cmd parses .bat with the current code page, so
rem Chinese text here would be mangled. The Chinese file name is fine (NTFS).
setlocal
chcp 65001 >nul
set "ROOT=%~dp0"
set "PYTHONPATH=%ROOT%src"
set "PYTHONIOENCODING=utf-8"
set "PY=python"
if exist "%ROOT%.venv\Scripts\python.exe" set "PY=%ROOT%.venv\Scripts\python.exe"
pushd "%ROOT%"
"%PY%" -m insightflow.gui
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" (
  echo.
  echo InsightFlow GUI exited with code %CODE%.
  echo See logs\insightflow.log for details.
  pause
)
popd
exit /b %CODE%
