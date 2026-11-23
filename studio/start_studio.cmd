@echo off
setlocal
set "PY=F:\python_env\a2a_env\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" -B "%~dp0server.py" %*
if errorlevel 1 pause
