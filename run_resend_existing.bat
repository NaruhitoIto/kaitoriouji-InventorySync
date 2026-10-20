@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === Re-send existing CSV files (no re-fetch) ===
python wasabi_stock_sync.py --resume
echo.
echo Finished. Check the log in the logs folder.
pause
