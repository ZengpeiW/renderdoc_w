@echo off
setlocal

where pwsh.exe >nul 2>nul
if errorlevel 1 (
  echo [ERROR] PowerShell 7 ^(pwsh.exe^) is required.
  exit /b 1
)

pwsh.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_android.ps1" %*
exit /b %ERRORLEVEL%
