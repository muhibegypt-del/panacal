@echo off
cd /d "%~dp0"
where py >nul 2>&1
if %errorlevel%==0 (py -3 crt_low_slider.py --diagnose) else (python crt_low_slider.py --diagnose)
