@echo off
rem NOTE: keep this file ASCII-only.
rem Yahoo! Shopping API: log in with Yahoo! JAPAN ID and save the access token.
chcp 65001 >nul
cd /d "%~dp0"
python yahoo_auth.py
echo.
pause
