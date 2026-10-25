@echo off
setlocal
set "PYTHONUTF8=1"
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo Please run setup.cmd first.
  pause
  exit /b 1
)
"%~dp0.venv\Scripts\python.exe" "%~dp0transcribe.py" %*
set "result=%errorlevel%"
if not defined WHISPER_NO_PAUSE pause
exit /b %result%
