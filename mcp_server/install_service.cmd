@echo off
rem Install the OTHVM MCP server as an NSSM service. Run from an elevated shell.
setlocal
set "APP=%~dp0"
set "PY=%APP%.venv\Scripts\python.exe"

if not exist "%PY%" (
  echo Missing Python virtualenv: %PY%
  exit /b 1
)

set "NSSM=%NSSM_EXE%"
if not defined NSSM if exist "%ProgramData%\chocolatey\bin\nssm.exe" set "NSSM=%ProgramData%\chocolatey\bin\nssm.exe"
if not defined NSSM if exist "%ProgramData%\chocolatey\lib\NSSM\tools\nssm.exe" set "NSSM=%ProgramData%\chocolatey\lib\NSSM\tools\nssm.exe"
if not defined NSSM set "NSSM=nssm.exe"

"%NSSM%" stop OTHVM-MCP >nul 2>&1
"%NSSM%" remove OTHVM-MCP confirm >nul 2>&1

"%NSSM%" install OTHVM-MCP "%PY%" "%APP%main.py"
"%NSSM%" set OTHVM-MCP AppDirectory "%APP%"
"%NSSM%" set OTHVM-MCP DisplayName "OTHVM Laptop Control MCP"
"%NSSM%" set OTHVM-MCP Description "REST tool server giving the OTHVM agent control over this Windows laptop (service role, port 8765)."
"%NSSM%" set OTHVM-MCP Start SERVICE_AUTO_START
"%NSSM%" set OTHVM-MCP ObjectName LocalSystem
"%NSSM%" set OTHVM-MCP AppStdout "%APP%logs\service.out.log"
"%NSSM%" set OTHVM-MCP AppStderr "%APP%logs\service.err.log"
"%NSSM%" set OTHVM-MCP AppRotateFiles 1
"%NSSM%" set OTHVM-MCP AppRotateOnline 1
"%NSSM%" set OTHVM-MCP AppRotateBytes 10485760
"%NSSM%" set OTHVM-MCP AppExit Default Restart
"%NSSM%" set OTHVM-MCP AppRestartDelay 5000
"%NSSM%" set OTHVM-MCP AppThrottle 3000

"%NSSM%" start OTHVM-MCP
timeout /t 4 /nobreak >nul
sc query OTHVM-MCP
