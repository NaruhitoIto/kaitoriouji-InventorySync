@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 1) 楽天の注文を取得して、ワサビの在庫を0にする（FTP送信）
python rakuten_orders_sync.py
rem 2) ワサビの在庫を楽天へ反映する（差分・自動）
python wasabi_stock_sync.py --auto
rem ※ Amazon は WS の連携で動いているため、ここでは実行しない。
rem    WS の Amazon 連携が止まったときだけ、手動で次を実行する:
rem      python amazon_orders_sync.py      （Amazonの注文 → WS在庫0）
rem      python amazon_stock_sync.py       （WS在庫 → Amazon。初回は Amazon の出品レポートと照合）
