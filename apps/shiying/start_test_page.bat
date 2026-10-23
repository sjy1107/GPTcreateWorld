@echo off
setlocal
cd /d "%~dp0"
".venv\Scripts\python.exe" -m tests.serve_demo
if errorlevel 1 pause
