@echo off
cd /d "%~dp0"
echo This returns the TV's current picture mode to its factory white balance and LUTs.
pause
where py >nul 2>&1
if %errorlevel%==0 (py -3 lg_autocal.py undo) else (python lg_autocal.py undo)
pause
