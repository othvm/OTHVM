@echo off
rem Watchdog: make sure the desktop helper (port 8766) is running in the user session.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0watchdog_desktop.ps1"
