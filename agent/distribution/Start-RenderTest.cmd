@echo off
setlocal DisableDelayedExpansion
set "RENDERTEST_MCP_PYTHON=%~dp0runtime\python\python.exe"
set "PATH=%~dp0;%~dp0runtime\python;%PATH%"
"%RENDERTEST_MCP_PYTHON%" "%~dp0tools\check_environment.py" --quiet
if errorlevel 1 (
  echo The bundled runtime check failed. Open README.html for help.
  pause
  exit /b 1
)
start "" "%~dp0qrendertest.exe" %*
endlocal
