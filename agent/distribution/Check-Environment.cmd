@echo off
setlocal DisableDelayedExpansion
"%~dp0runtime\python\python.exe" "%~dp0tools\check_environment.py"
set "RESULT=%ERRORLEVEL%"
pause
exit /b %RESULT%
