@echo off
cd /d "%~dp0"
where py >nul 2>&1 && (py -3 lg_autocal.py %*) || (python lg_autocal.py %*)
pause
