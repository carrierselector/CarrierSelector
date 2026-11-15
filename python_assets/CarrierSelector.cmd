@echo off
setlocal
chcp 65001 >nul
title CarrierSelector v.6.1 (Python)
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
where py >nul 2>nul
if not errorlevel 1 (
  py -3 "%~dp0start.py"
) else (
  python "%~dp0start.py"
)
set "APP_EXIT=%ERRORLEVEL%"
echo.
if not "%APP_EXIT%"=="0" echo CarrierSelector exited with code %APP_EXIT%. If Python is missing, install it from python.org.
pause
exit /b %APP_EXIT%
