@echo off
rem Install Telegram Personal as an NSSM service. Run from an elevated shell.
setlocal
set "APP=%~dp0"
set "PY=%APP%.venv\Scripts\python.exe"

if not exist "%PY%" (
  echo Missing Python virtualenv: %PY%
  echo Create it with: python -m venv "%APP%.venv"
  exit /b 1
)

set "NSSM=%NSSM_EXE%"
if not defined NSSM if exist "%ProgramData%\chocolatey\bin\nssm.exe" set "NSSM=%ProgramData%\chocolatey\bin\nssm.exe"
if not defined NSSM if exist "%ProgramData%\chocolatey\lib\NSSM\tools\nssm.exe" set "NSSM=%ProgramData%\chocolatey\lib\NSSM\tools\nssm.exe"
if not defined NSSM set "NSSM=nssm.exe"

"%NSSM%" stop OTHVM-TelegramPersonal >nul 2>&1
"%NSSM%" remove OTHVM-TelegramPersonal confirm >nul 2>&1

"%NSSM%" install OTHVM-TelegramPersonal "%PY%" "%APP%main.py"
"%NSSM%" set OTHVM-TelegramPersonal AppDirectory "%APP%"
"%NSSM%" set OTHVM-TelegramPersonal DisplayName "OTHVM Telegram Personal (Telethon)"
"%NSSM%" set OTHVM-TelegramPersonal Description "Local REST bridge to a personal Telegram account. Sends are rate-limited and require explicit confirmation."
"%NSSM%" set OTHVM-TelegramPersonal Start SERVICE_AUTO_START
"%NSSM%" set OTHVM-TelegramPersonal ObjectName LocalSystem
"%NSSM%" set OTHVM-TelegramPersonal AppStdout "%APP%logs\service.out.log"
"%NSSM%" set OTHVM-TelegramPersonal AppStderr "%APP%logs\service.err.log"
"%NSSM%" set OTHVM-TelegramPersonal AppRotateFiles 1
"%NSSM%" set OTHVM-TelegramPersonal AppRotateOnline 1
"%NSSM%" set OTHVM-TelegramPersonal AppRotateBytes 10485760
"%NSSM%" set OTHVM-TelegramPersonal AppExit Default Restart
"%NSSM%" set OTHVM-TelegramPersonal AppRestartDelay 5000
"%NSSM%" set OTHVM-TelegramPersonal AppThrottle 3000

if not exist "%APP%logs" mkdir "%APP%logs"
"%NSSM%" start OTHVM-TelegramPersonal
timeout /t 4 /nobreak >nul
sc query OTHVM-TelegramPersonal
