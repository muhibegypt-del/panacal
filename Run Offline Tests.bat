@echo off
cd /d "%~dp0"
where py >nul 2>&1
if %errorlevel%==0 (py -3 -m unittest discover -s tests -t . -v) else (python -m unittest discover -s tests -t . -v)
pause
