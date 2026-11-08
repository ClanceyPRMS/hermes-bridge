@echo off
rem Hermes bridge - Windows launcher
rem Usage: hermes-bridge.cmd status ^| push ^| pull [--dry-run] [--apply]
rem Keep pure ASCII with CRLF endings: non-ASCII bytes make cmd.exe mis-parse
rem this file under a non-UTF8 OEM code page (e.g. 936) and lose line prefixes.
setlocal
if "%~1"=="" goto :usage
set "VAULT=%~dp0"
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY (
  for /d %%D in ("%LOCALAPPDATA%\hermes\tools\python-*") do (
    if exist "%%~fD\python.exe" set "PY=%%~fD\python.exe"
  )
)
if not defined PY (
  echo [ERROR] Python not found. Install Hermes Desktop or add python to PATH.
  exit /b 1
)
"%PY%" "%VAULT%hermes-bridge.py" %*
set "RC=%ERRORLEVEL%"
endlocal & exit /b %RC%

:usage
echo Usage: hermes-bridge.cmd ^<status^|push^|pull^> [--dry-run] [--apply]
echo   status   show the diff between this machine and the vault
echo   push     run this before leaving this machine
echo   pull     run this after arriving on this machine
pause
exit /b 1
