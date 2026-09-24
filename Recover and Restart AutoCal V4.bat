@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0recover_autocal.ps1"
if errorlevel 1 (
    echo.
    echo Recovery did not complete. See the error above.
    pause
    exit /b 1
)
echo.
echo Re-arm the TV as described above, then follow AutoCal's prompt below.
echo.
python autocal.py run
pause
