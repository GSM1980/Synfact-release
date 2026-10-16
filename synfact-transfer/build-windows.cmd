@echo off
setlocal
cd /d "%~dp0"
if not exist .venv-build\Scripts\python.exe (
  py -3.12 -m venv .venv-build
  if errorlevel 1 exit /b 1
)
.venv-build\Scripts\python.exe -m pip install -r requirements-build.txt
if errorlevel 1 exit /b 1
.venv-build\Scripts\python.exe build.py %*
exit /b %errorlevel%
