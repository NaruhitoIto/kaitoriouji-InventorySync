@echo off
rem NOTE: keep this file ASCII-only. cmd.exe misreads multi-byte (Japanese) lines
rem after "chcp 65001" and runs fragments of them as commands.
chcp 65001 >nul
cd /d "%~dp0"
rem 1) Rakuten orders -> set Wasabi stock to 0 (FTP)
python rakuten_orders_sync.py
rem 2) Wasabi stock -> Rakuten (diff, auto)
python wasabi_stock_sync.py --auto
rem Amazon is synced by Wasabi itself, so it is NOT run here.
rem Only when the Wasabi Amazon sync is stopped, run these by hand:
rem   amazon_orders_sync.py  (Amazon orders -> Wasabi stock 0)
rem   amazon_stock_sync.py   (Wasabi stock -> Amazon)
