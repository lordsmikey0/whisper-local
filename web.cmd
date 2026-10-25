@echo off
setlocal
set "PYTHONUTF8=1"
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Please run setup.cmd first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" serve.py %*
set "result=%errorlevel%"
if not "%result%"=="0" pause
exit /b %result%
