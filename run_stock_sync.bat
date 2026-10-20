@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === Wasabi stock sync (manual full run) ===
python wasabi_stock_sync.py
echo.
echo Finished. Check the log in the logs folder.
pause
