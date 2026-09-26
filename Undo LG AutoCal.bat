@echo off
cd /d "%~dp0"
echo This clears the current picture mode's white balance and LUTs (greyscale and colour calibration).
echo It does not bring back LG's factory colours: run LG AutoCal afterwards to calibrate again.
pause
where py >nul 2>&1
if %errorlevel%==0 (py -3 lg_autocal.py undo) else (python lg_autocal.py undo)
pause
