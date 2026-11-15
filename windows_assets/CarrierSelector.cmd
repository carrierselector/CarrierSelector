@echo off
setlocal
chcp 65001 >nul
title CarrierSelector v.6.3

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
if not exist "%~dp0runtime\python.exe" goto no_runtime
if not exist "%~dp0app\launcher.py" goto no_launcher
"%~dp0runtime\python.exe" "%~dp0app\launcher.py"
set "APP_EXIT=%ERRORLEVEL%"
if not defined APP_EXIT set "APP_EXIT=1"
echo.
if not "%APP_EXIT%"=="0" echo CarrierSelector exited with code %APP_EXIT%.
pause
exit /b %APP_EXIT%

:no_runtime
echo ERROR: runtime\python.exe was not found. Extract the entire ZIP first.
echo.
pause
exit /b 11

:no_launcher
echo ERROR: app\launcher.py was not found. Extract the entire ZIP first.
echo.
pause
exit /b 12
