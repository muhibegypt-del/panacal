@echo off
cd /d "%~dp0"
where pyw >nul 2>&1
if %errorlevel%==0 (start "" pyw -3 crt_low_slider.py %*) else (start "" pythonw crt_low_slider.py %*)
