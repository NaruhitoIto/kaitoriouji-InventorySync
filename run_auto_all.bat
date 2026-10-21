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
rem Mercari Shops is also synced by Wasabi for now, so it is NOT run here either.
rem After switching off the Wasabi Mercari sync (see the header of each script):
rem   add mercari_orders_sync.py right after step 1   (Mercari orders -> Wasabi stock 0)
rem   add mercari_stock_sync.py --auto after step 2   (Wasabi stock -> Mercari)
rem   and set snapshot_malls in wasabi_config.ini so step 2 saves the stock for Mercari
