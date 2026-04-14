@echo off
title Bookwave
echo Installing packages...
pip install -r "%~dp0requirements.txt" -q
echo Starting Bookwave...
python "%~dp0app.py"
pause
