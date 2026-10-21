@echo off
rem NOTE: keep this file ASCII-only (cmd.exe misreads multi-byte lines after chcp 65001).
rem Mercari: drop "product_update_import_result*.csv" files onto this file to rebuild the
rem stock update file without the failed rows. Double-click = newest result in Downloads.
chcp 65001 >nul
cd /d "%~dp0"
python mercari_retry.py %*
echo.
pause
