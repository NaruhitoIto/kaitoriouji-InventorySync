# -*- coding: utf-8 -*-
"""
メルカリShops API の注文（orderTransactions）から、前回以降に作成・更新された注文を取得し、
その商品（SKU）の在庫を 0 にするファイルをワサビスイッチのFTP（/stock_import）へ送る。
（rakuten_orders_sync.py / amazon_orders_sync.py のメルカリ版）

使い方:
  python mercari_orders_sync.py --dry-run   # ファイルは作るが、FTPへは送らない（まず必ずこれで確認）
  python mercari_orders_sync.py             # 送信する（FTP）
  python mercari_orders_sync.py --until 2026-11-01T09:00:00+09:00
                                            # 注文日時がこの時刻までの注文だけを対象にする（WS連携からの切り替え時など）

mercari_shops_config.ini の orders_last_checked に「この時刻以降の注文を対象にする」初回の時刻を入れてください。
以後はスクリプトが mercari_orders_state.json で管理します。
"""
import configparser
import ftplib
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mercari_shops_api import BASE_DIR, gql, load_mercari_config, rate_summary

WASABI_FTP_CFG = BASE_DIR / "wasabi_ftp_config.ini"
STATE_FILE = BASE_DIR / "mercari_orders_state.json"
OUT_DIR = BASE_DIR / "stock_csv"
LOG_DIR = BASE_DIR / "logs"
JST = timezone(timedelta(hours=9))
# 在庫を減らす対象の取引状態（CANCELED / CANCELING は除く。コンビニ払い待ちも確保のため含める）
ACTIVE_STATUSES = ["WAITING_FOR_PAYMENT", "WAITING_FOR_SHIPPING", "COMPLETING", "COMPLETED"]
OVERLAP = timedelta(minutes=10)  # 取得期間を前回と少し重ねる（処理済み注文番号で重複は除く）
PAGE_SIZE = 50
KEEP_PROCESSED = 20000  # 処理済み注文番号の保持上限

LOG_DIR.mkdir(exist_ok=True)
OUT_DIR.mkdir(exist_ok=True)
log_file = LOG_DIR / f"mercari_orders_{datetime.now(JST):%Y%m%d_%H%M%S}.log"
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s",
                    handlers=[logging.FileHandler(log_file, encoding="utf-8"), logging.StreamHandler(sys.stdout)])
log = logging.getLogger("mercari_orders_sync")

ORDERS_QUERY = """
query orderTransactions($first: Int, $after: String, $statuses: [OrderTransactionStatusFilter!],
                        $updatedDateGte: DateTime, $updatedDateLt: DateTime) {
  orderTransactions(first: $first, after: $after, statuses: $statuses,
                    updatedDateGte: $updatedDateGte, updatedDateLt: $updatedDateLt) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id
        status
        createdAt
        products {
          purchasedQuantity
          unshippedCanceledQuantity
          unshippedCancelingQuantity
          shippedCanceledQuantity
          shippedCancelingQuantity
          variant { skuCode }
        }
      }
    }
  }
}
"""


def load_wasabi_ftp():
    w = configparser.ConfigParser()
    w.read(WASABI_FTP_CFG, encoding="utf-8")
    return w["WASABI_FTP"]


def load_state(initial):
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    if not initial:
        raise SystemExit("mercari_shops_config.ini の orders_last_checked（初回の取得開始時刻）を設定してください")
    return {"last_checked": initial, "processed": []}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def iso_utc(dt):
    # API の DateTime は UTC（日本時間と9時間ずれる）
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def search_orders(mcfg, start, end):
    """期間内（最終更新日時基準）の有効な取引を全ページ取得する。"""
    orders, after = [], None
    while True:
        data = gql(mcfg, ORDERS_QUERY, {
            "first": PAGE_SIZE, "after": after, "statuses": ACTIVE_STATUSES,
            "updatedDateGte": iso_utc(start), "updatedDateLt": iso_utc(end),
        })
        conn = data.get("orderTransactions") or {}
        orders.extend(e["node"] for e in conn.get("edges") or [] if e.get("node"))
        page = conn.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            return orders
        after = page.get("endCursor")


def active_quantity(p):
    """キャンセル済み・キャンセル処理中を除いた数量。"""
    return (p.get("purchasedQuantity", 0) - p.get("unshippedCanceledQuantity", 0) - p.get("unshippedCancelingQuantity", 0)
            - p.get("shippedCanceledQuantity", 0) - p.get("shippedCancelingQuantity", 0))


def order_skus(order):
    """取引1件の、キャンセルされていない商品のSKU（商品管理コード）一覧。"""
    skus = []
    for p in order.get("products") or []:
        sku = ((p.get("variant") or {}).get("skuCode") or "").strip()
        if not sku:
            log.warning(f"SKU（商品管理コード）が無い商品があります: 取引 {order.get('id')}")
            continue
        if active_quantity(p) > 0:
            skus.append(sku)
    return skus


def write_stock_csv(skus, newline):
    path = OUT_DIR / f"wasabi_stock_mercari_{datetime.now(JST):%Y%m%d_%H%M%S}.csv"
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
    mcfg = load_mercari_config()
    wf = load_wasabi_ftp()
    now = datetime.now(JST) - timedelta(minutes=1)
    state = load_state(mcfg["orders_last_checked"])
    start = datetime.fromisoformat(state["last_checked"])
    log.info(f"モード: {'dry-run（FTP送信なし）' if dry else '送信'} / 環境: {mcfg['environment']} / 取得期間: {start} 〜 {now}")

    orders = search_orders(mcfg, start - OVERLAP, now)
    processed = set(state["processed"])
    new_orders = [o for o in orders if o.get("id") and o["id"] not in processed]
    log.info(f"取得した取引: {len(orders)}件 / 未処理: {len(new_orders)}件 / {rate_summary()}")
    if until:
        # 期間は「最終更新日時」で取るため、締め時刻前の注文で後から更新されたものも拾える。注文日時で締める
        before = len(new_orders)
        new_orders = [o for o in new_orders if parse_dt(o["createdAt"]) <= until]
        log.info(f"注文日時 {until} までに絞り込み: {len(new_orders)}件（締め時刻より後の注文 {before - len(new_orders)}件は対象外）")

    skus, used = set(), []
    for o in new_orders:
        skus.update(order_skus(o))
        used.append(o["id"])
    log.info(f"在庫0にする対象SKU: {len(skus)}件 / 対象取引: {len(used)}件")

    if not skus:
        state["processed"] = (state["processed"] + used)[-KEEP_PROCESSED:]
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
