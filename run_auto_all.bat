@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 1) 楽天の注文を取得して、ワサビの在庫を0にする（FTP送信）
python rakuten_orders_sync.py
rem 2) ワサビの在庫を楽天へ反映する（差分・自動）
python wasabi_stock_sync.py --auto
