@echo off
cd /d "%~dp0"
echo Starting TinyReadAloud (console mode — errors shown here)...
venv\Scripts\python.exe run.py
echo.
echo Exit code: %ERRORLEVEL%
echo Log file: %LOCALAPPDATA%\TinyReadAloud\tinyreadaloud.log
pause
