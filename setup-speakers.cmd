@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup-speakers.ps1"
set "result=%errorlevel%"
pause
exit /b %result%
