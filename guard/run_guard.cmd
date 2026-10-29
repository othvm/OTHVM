@echo off
rem Run the OTHVM guard with the Node.js interpreter available on this host.
setlocal
set "NODE_EXE=node"
if exist "%ProgramFiles%\nodejs\node.exe" set "NODE_EXE=%ProgramFiles%\nodejs\node.exe"
"%NODE_EXE%" "%~dp0guard.js" >> "%~dp0guard.out.log" 2>&1
