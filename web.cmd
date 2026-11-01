@echo off
setlocal
set "PYTHONUTF8=1"
rem This laptop has one NVIDIA CUDA GPU: index 0 (Intel graphics is not a CUDA GPU).
set "CUDA_VISIBLE_DEVICES=0"
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
