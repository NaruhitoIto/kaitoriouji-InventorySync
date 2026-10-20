# -*- coding: utf-8 -*-
"""
Amazon SP-API の注文APIから、前回以降の注文を取得し、
その商品（SKU）の在庫を 0 にするファイルをワサビスイッチのFTP（/stock_import）へ送る。
（rakuten_orders_sync.py のAmazon版。WS が 0 になれば、楽天側は wasabi_stock_sync.py が次回反映する）

使い方:
  python amazon_orders_sync.py --dry-run   # ファイルは作るが、FTPへは送らない（まず必ずこれで確認）
  python amazon_orders_sync.py             # 送信する（FTP）
  python amazon_orders_sync.py --until 2026-10-20T21:30:00+09:00
                                           # 購入日時がこの時刻までの注文だけを対象にする（WS連携の再開時など）

amazon_sp_api_config.ini の orders_last_checked に「この時刻以降の注文を対象にする」初回の時刻
（例 2026-10-20T19:00:00+09:00）を入れてください。以後はスクリプトが amazon_orders_state.json で管理します。
"""
import configparser
import ftplib
import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from amazon_sp_api import BASE_DIR, load_amazon_config, sp_request

WASABI_FTP_CFG = BASE_DIR / "wasabi_ftp_config.ini"
STATE_FILE = BASE_DIR / "amazon_orders_state.json"
OUT_DIR = BASE_DIR / "stock_csv"
LOG_DIR = BASE_DIR / "logs"
JST = timezone(timedelta(hours=9))
# 在庫を減らす対象の注文状態（Canceled / Unfulfillable は除く。Pending も確保のため含める）
ACTIVE_STATUSES = "Pending,Unshipped,PartiallyShipped,Shipped"
KEEP_PROCESSED = 20000  # 処理済み注文番号の保持上限

LOG_DIR.mkdir(exist_ok=True)
OUT_DIR.mkdir(exist_ok=True)
log_file = LOG_DIR / f"amazon_orders_{datetime.now(JST):%Y%m%d_%H%M%S}.log"
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s",
                    handlers=[logging.FileHandler(log_file, encoding="utf-8"), logging.StreamHandler(sys.stdout)])
log = logging.getLogger("amazon_orders_sync")


def load_wasabi_ftp():
    w = configparser.ConfigParser()
    w.read(WASABI_FTP_CFG, encoding="utf-8")
    return w["WASABI_FTP"]


def load_state(initial):
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    if not initial:
        raise SystemExit("amazon_sp_api_config.ini の orders_last_checked（初回の取得開始時刻）を設定してください")
    return {"last_checked": initial, "processed": []}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def iso_utc(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def search_orders(acfg, start, end):
    """期間内（最終更新日時基準）の有効な注文を全ページ取得する。"""
    orders, params = [], {
        "MarketplaceIds": acfg["marketplace_id"],
        "LastUpdatedAfter": iso_utc(start),
        "LastUpdatedBefore": iso_utc(end),
        "OrderStatuses": ACTIVE_STATUSES,
        "MaxResultsPerPage": 100,
    }
    while True:
        j = sp_request(acfg, "GET", "/orders/v0/orders", params=params)
        payload = j.get("payload", {})
        orders.extend(payload.get("Orders", []))
        token = payload.get("NextToken")
        if not token:
            return orders
        params = {"NextToken": token}
        time.sleep(1)


def order_skus(acfg, order_id):
    """注文1件の出品者SKU一覧。"""
    skus, params = [], None
    while True:
        j = sp_request(acfg, "GET", f"/orders/v0/orders/{order_id}/orderItems", params=params)
        payload = j.get("payload", {})
        skus.extend(i["SellerSKU"].strip() for i in payload.get("OrderItems", []) if i.get("SellerSKU"))
        token = payload.get("NextToken")
        if not token:
            return skus
        params = {"NextToken": token}
        time.sleep(2)


def write_stock_csv(skus, newline):
    path = OUT_DIR / f"wasabi_stock_amazon_{datetime.now(JST):%Y%m%d_%H%M%S}.csv"
    eol = "\r\n" if newline.upper() == "CRLF" else "\n"
    lines = ["コントロールカラム,商品番号,在庫数"] + [f"u,{s},0" for s in sorted(skus)]
    path.write_bytes((eol.join(lines) + eol).encode("cp932", errors="replace"))
    return path


def upload_ftp(wf, path):
    """一時名でアップロードしてから正式名にリネームする（取込の途中読みを防ぐ）。"""
    tmp = path.name + ".tmp"
    with ftplib.FTP() as ftp:
        ftp.connect(wf["host"], int(wf.get("port", "21") or 21), timeout=60)
        ftp.login(wf["username"], wf["password"])
        ftp.cwd(wf["remote_dir"])
        with open(path, "rb") as f:
            ftp.storbinary(f"STOR {tmp}", f)
        ftp.rename(tmp, path.name)
    log.info(f"ワサビFTPへ送信: {wf['remote_dir']}/{path.name}")


def main():
    dry = "--dry-run" in sys.argv
    until = None
    if "--until" in sys.argv:
        until = datetime.fromisoformat(sys.argv[sys.argv.index("--until") + 1])
        if until.tzinfo is None:
            until = until.replace(tzinfo=JST)
    acfg = load_amazon_config()
    wf = load_wasabi_ftp()
    now = datetime.now(JST) - timedelta(minutes=3)  # SP-API は LastUpdatedBefore を直近2分より前にする必要がある
    state = load_state(acfg["orders_last_checked"])
    start = datetime.fromisoformat(state["last_checked"])
    log.info(f"モード: {'dry-run（FTP送信なし）' if dry else '送信'} / 取得期間: {start} 〜 {now}")

    orders = search_orders(acfg, start, now)
    processed = set(state["processed"])
    new_orders = [o for o in orders if o.get("AmazonOrderId") and o["AmazonOrderId"] not in processed]
    log.info(f"取得した注文: {len(orders)}件 / 未処理: {len(new_orders)}件")
    if until:
        # 期間は「最終更新日時」で取るため、締め時刻前の注文で後から更新されたものも拾える。購入日時で締める
        before = len(new_orders)
        new_orders = [o for o in new_orders if datetime.fromisoformat(o["PurchaseDate"].replace("Z", "+00:00")) <= until]
        log.info(f"購入日時 {until} までに絞り込み: {len(new_orders)}件（締め時刻より後の注文 {before - len(new_orders)}件は対象外）")

    skus, used = set(), []
    for o in new_orders:
        oid = o["AmazonOrderId"]
        try:
            skus.update(order_skus(acfg, oid))
        except Exception as e:
            # 取得できなかった注文は処理済みにせず、次回に回す（期間の起点も進めない）
            log.error(f"注文明細の取得に失敗: {oid} / {e}")
            raise SystemExit("注文明細の取得に失敗したため中止しました（状態は更新していません）")
        used.append(oid)
        time.sleep(2)  # getOrderItems は約0.5リクエスト/秒
    log.info(f"在庫0にする対象SKU: {len(skus)}件 / 対象注文: {len(used)}件")

    if not skus:
        state["last_checked"] = now.isoformat()
        if not dry:
            save_state(state)
        log.info("対象がないので終了します")
        return

    path = write_stock_csv(skus, wf.get("newline", "LF"))
    log.info(f"在庫ファイルを作成: {path.name}（{len(skus)}件）")
    if dry:
        log.info("dry-run のため、FTPへは送っていません。ファイルの中身を確認してください。")
        return

    upload_ftp(wf, path)  # 失敗時は例外で止まり、状態は更新しない
    state["processed"] = (state["processed"] + used)[-KEEP_PROCESSED:]
    state["last_checked"] = now.isoformat()
    save_state(state)
    log.info("送信と状態の更新が完了しました")


if __name__ == "__main__":
    main()
