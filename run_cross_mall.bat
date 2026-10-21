@echo off
rem NOTE: keep this file ASCII-only (cmd.exe misreads multi-byte lines after chcp 65001).
rem While Wasabi is down: zero the stock of sold items directly between malls
rem (Rakuten <-> Amazon by API, Mercari / Yahoo order files from the inbox folder).
chcp 65001 >nul
cd /d "%~dp0"
python cross_mall_sync.py
