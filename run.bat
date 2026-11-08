@echo off
setlocal
chcp 65001 >nul
set "PYTHONPATH=%~dp0src"
set "PYTHONIOENCODING=utf-8"
pushd "%~dp0"
python -m insightflow %*
set "CODE=%ERRORLEVEL%"
popd
exit /b %CODE%
