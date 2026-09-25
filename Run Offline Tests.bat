@echo off
cd /d "%~dp0"
where py >nul 2>&1 && (py -3 -m unittest discover -s tests -t . -v) || (python -m unittest discover -s tests -t . -v)
pause
