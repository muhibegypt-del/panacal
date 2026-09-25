@echo off
cd /d "%~dp0"
python -m unittest discover -s tests -t . -v
pause
