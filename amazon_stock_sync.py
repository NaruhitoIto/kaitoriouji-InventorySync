# -*- coding: utf-8 -*-
"""
ワサビスイッチ(WS)の在庫を Amazon（自己発送 MFN）へ SP-API で反映するツール。
在庫は 0 / 1 の二値。前回送信からの差分だけを JSON_LISTINGS_FEED で送る。

初回（または --full）は Amazon の出品レポートで「Amazon上の実在庫」を取得し、WS と照合して基準を作る。
送信状態（amazon_stock_state.json）のキーは「Amazonに出品が存在するSKU」。存在しないSKUには送らない。
WS は在庫0になると Amazon店の状態を「出品中」以外に変えるため、「在庫1 かつ 出品中」だけを在庫1として扱う。

使い方:
  python amazon_stock_sync.py --test       認証と出品者情報の確認（在庫は送らない）
  python amazon_stock_sync.py --dry-run    差分を計算して一覧を表示するだけ（送信しない）
  python amazon_stock_sync.py --validate   先頭1SKUをAmazonの検証モードで確認（在庫は変わらない）
  python amazon_stock_sync.py              通常実行（差分を確認 → 送信）
  python amazon_stock_sync.py --auto       無確認の自動実行（基準データが必要）
  python amazon_stock_sync.py --full       Amazonの出品レポートと照合して基準を作り直す（--full --dry-run も可）

WS の設定は wasabi_stock_sync.py と同じ wasabi_config.ini を使う。追加できる項目（[WASABI]）:
  amazon_mall_name = Amazon店  ← WS の「出品状況」に出るAmazonのモール名
  amazon_status    = 出品中
"""
import configparser
import gzip
import json
import sys
import time
from datetime import datetime
from urllib.parse import quote

import requests

import wasabi_stock_sync as ws
from amazon_sp_api import BASE_DIR, load_amazon_config, sp_request

log = ws.log
STATE_FILE = BASE_DIR / "amazon_stock_state.json"
LOCK_FILE = BASE_DIR / "amazon_stock_sync.lock"
CHUNK_SIZE = 5000          # 1フィードあたりの件数
AUTO_MAX_CHANGES = 20000   # 自動実行で一度に送る上限
PRODUCT_TYPE = "PRODUCT"
CONTENT_TYPE = "application/json; charset=UTF-8"


def load_ws_amazon_cfg():
    cfg = ws.load_configs()
    wc = configparser.ConfigParser()
    wc.read(ws.WASABI_CONFIG, encoding="utf-8")
    cfg["amazon_mall_name"] = wc.get("WASABI", "amazon_mall_name", fallback="Amazon店").strip()
    cfg["amazon_status"] = wc.get("WASABI", "amazon_status", fallback="出品中").strip()
    return cfg


def is_amazon_listed(item, cfg):
    return ws.listing_status(item, cfg["amazon_mall_name"]) == cfg["amazon_status"]


def collect_amazon_stock(cfg, query=None, max_pages=None):
    """Amazon で「出品中」の商品の {SKU(そのままの表記): 在庫(0/1)} を作る。"""
    stock, skipped, bad = {}, 0, 0
    for item in ws.iter_products(cfg, max_pages=max_pages, query=query):
        if not is_amazon_listed(item, cfg):
            skipped += 1
            continue
        sku = str(item.get("商品番号") or "").strip()
        if not sku:
            continue
        qty = ws.parse_stock(item.get("在庫数"))
        if qty is None:
            bad += 1
            log.warning(f"在庫数が数値ではないためスキップ: {sku} ({item.get('在庫数')!r})")
            continue
        stock[sku] = 1 if qty > 0 else 0
    log.info(f"Amazon対象SKU: {len(stock):,}件 / Amazon出品中以外でスキップ: {skipped:,}件 / 在庫数不正: {bad:,}件")
    return stock


def load_state():
    return json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}


def save_state(state):
    data = json.dumps(state, ensure_ascii=False)
    for attempt in range(6):
        try:
            STATE_FILE.write_text(data, encoding="utf-8")
            return
        except PermissionError:
            log.warning(f"送信状態ファイルを書き込めません。再試行 {attempt + 1}/6")
            time.sleep(5)
    raise RuntimeError("amazon_stock_state.json を保存できませんでした")


def build_patch(qty):
    return [{
        "op": "replace",
        "path": "/attributes/fulfillment_availability",
        "value": [{"fulfillment_channel_code": "DEFAULT", "quantity": int(qty)}],
    }]


def submit_feed(acfg, part):
    """{sku: qty} を1フィードで送り、処理結果を待つ。戻り値は (成功SKUの集合, 失敗 {sku: メッセージ})。"""
    skus = list(part)
    messages = [
        {"messageId": i, "sku": sku, "operationType": "PATCH",
         "productType": PRODUCT_TYPE, "patches": build_patch(part[sku])}
        for i, sku in enumerate(skus, 1)
    ]
    body = json.dumps(
        {"header": {"sellerId": acfg["seller_id"], "version": "2.0", "issueLocale": "ja_JP"}, "messages": messages},
        ensure_ascii=False,
    ).encode("utf-8")

    doc = sp_request(acfg, "POST", "/feeds/2021-06-30/documents", json_body={"contentType": CONTENT_TYPE})
    up = requests.put(doc["url"], data=body, headers={"Content-Type": CONTENT_TYPE}, timeout=300)
    if up.status_code not in (200, 201):
        raise RuntimeError(f"フィードのアップロードに失敗 HTTP {up.status_code}")
    feed = sp_request(acfg, "POST", "/feeds/2021-06-30/feeds", json_body={
        "feedType": "JSON_LISTINGS_FEED",
        "marketplaceIds": [acfg["marketplace_id"]],
        "inputFeedDocumentId": doc["feedDocumentId"],
    })
    feed_id = feed["feedId"]
    log.info(f"フィードを送信しました: feedId={feed_id}（{len(skus):,}件）。処理完了を待ちます")

    for _ in range(120):  # 最大 約60分
        time.sleep(30)
        st = sp_request(acfg, "GET", f"/feeds/2021-06-30/feeds/{feed_id}")
        status = st.get("processingStatus")
        if status in ("DONE", "CANCELLED", "FATAL"):
            break
    else:
        raise RuntimeError(f"フィード処理が時間内に終わりませんでした: feedId={feed_id}")
    if status != "DONE":
        raise RuntimeError(f"フィード処理が {status} で終了しました: feedId={feed_id}")

    failed = {}
    result_id = st.get("resultFeedDocumentId")
    if result_id:
        rd = sp_request(acfg, "GET", f"/feeds/2021-06-30/documents/{result_id}")
        rr = requests.get(rd["url"], timeout=300)
        rr.raise_for_status()
        report = rr.json()
        log.info(f"処理結果サマリー: {report.get('summary')}")
        for issue in report.get("issues", []):
            if str(issue.get("severity", "")).upper() == "ERROR":
                sku = skus[issue["messageId"] - 1] if 1 <= issue.get("messageId", 0) <= len(skus) else "?"
                failed[sku] = f"{issue.get('code')}: {issue.get('message')}"
    ok = set(skus) - set(failed)
    return ok, failed


def run_test(acfg):
    j = sp_request(acfg, "GET", "/sellers/v1/marketplaceParticipations")
    rows = j.get("payload", [])
    log.info(f"認証OK。参加マーケットプレイス: {len(rows)}件")
    for row in rows:
        m = row.get("marketplace", {})
        log.info(f"  {m.get('id')} {m.get('countryCode')} {m.get('name')} / 出品可否: {row.get('participation', {}).get('isParticipating')}")
    log.info(f"設定中のマーケットプレイスID: {acfg['marketplace_id']}")


def run_validate(acfg, changed):
    """先頭1SKUを VALIDATION_PREVIEW で確認する（実際の在庫は変わらない）。"""
    sku, qty = next(iter(changed.items()))
    path = f"/listings/2021-08-01/items/{acfg['seller_id']}/{quote(sku, safe='')}"
    j = sp_request(acfg, "PATCH", path,
                   params={"marketplaceIds": acfg["marketplace_id"], "issueLocale": "ja_JP", "mode": "VALIDATION_PREVIEW"},
                   json_body={"productType": PRODUCT_TYPE, "patches": build_patch(qty)})
    log.info(f"検証結果 SKU={sku} qty={qty}: status={j.get('status')} issues={j.get('issues')}")


REPORT_TYPE = "GET_MERCHANT_LISTINGS_ALL_DATA"  # 出品中・停止中を含む全出品（SKU・数量）
SKU_COLS = ("seller-sku", "出品者SKU", "sku")
QTY_COLS = ("quantity", "数量", "在庫数")
CHANNEL_COLS = ("fulfillment-channel", "フルフィルメント・チャンネル", "フルフィルメントチャネル")


def fetch_amazon_listings(acfg):
    """出品レポートを作成・取得し、{SKU: Amazon上の在庫(0/1)} を返す（自己発送のみ）。"""
    rep = sp_request(acfg, "POST", "/reports/2021-06-30/reports",
                     json_body={"reportType": REPORT_TYPE, "marketplaceIds": [acfg["marketplace_id"]]})
    report_id = rep["reportId"]
    log.info(f"Amazonの出品レポートを作成中: reportId={report_id}")
    for _ in range(120):  # 最大 約60分
        time.sleep(30)
        st = sp_request(acfg, "GET", f"/reports/2021-06-30/reports/{report_id}")
        status = st.get("processingStatus")
        if status in ("DONE", "CANCELLED", "FATAL"):
            break
    else:
        raise RuntimeError(f"出品レポートが時間内にできませんでした: reportId={report_id}")
    if status != "DONE":
        raise RuntimeError(f"出品レポートが {status} で終了しました: reportId={report_id}")

    doc = sp_request(acfg, "GET", f"/reports/2021-06-30/documents/{st['reportDocumentId']}")
    raw = requests.get(doc["url"], timeout=600).content
    if doc.get("compressionAlgorithm") == "GZIP":
        raw = gzip.decompress(raw)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp932", errors="replace")

    lines = text.splitlines()
    header = lines[0].split("\t")

    def col(names):
        for n in names:
            if n in header:
                return header.index(n)
        return None

    i_sku, i_qty, i_ch = col(SKU_COLS), col(QTY_COLS), col(CHANNEL_COLS)
    if i_sku is None or i_qty is None:
        raise RuntimeError(f"出品レポートの列名が想定と違います: {header}")
    listings, fba = {}, 0
    for line in lines[1:]:
        row = line.split("\t")
        if len(row) <= max(i_sku, i_qty):
            continue
        if i_ch is not None and len(row) > i_ch and row[i_ch].strip().upper().startswith("AMAZON"):
            fba += 1  # FBA は対象外
            continue
        sku = row[i_sku].strip()
        if sku:
            listings[sku] = 1 if (ws.parse_stock(row[i_qty]) or 0) > 0 else 0
    log.info(f"Amazonの自己発送出品: {len(listings):,}件（うち在庫あり {sum(listings.values()):,}件）/ FBAで除外: {fba:,}件")
    return listings


def compute_changed(cfg, state, in_stock):
    """state（Amazon上の在庫）と WS の「在庫1かつ出品中」を比べ、送るべき {SKU: 0/1} を返す。
    Amazonに出品が存在しないSKU（state に無いもの）には送らない。"""
    changed = {s: 1 for s in in_stock if s in state and state[s] != 1}
    dropped = [s for s, q in state.items() if q == 1 and s not in in_stock]
    for s in dropped:
        changed[s] = 0
    not_on_amazon = sum(1 for s in in_stock if s not in state)
    log.info(f"在庫1にする: {len(changed) - len(dropped):,}件 / 在庫0にする: {len(dropped):,}件 / "
             f"WSは在庫1だがAmazonに出品が無い（送らない）: {not_on_amazon:,}件")
    return changed


def main():
    args = set(sys.argv[1:])
    acfg = load_amazon_config()
    if "--test" in args:
        run_test(acfg)
        return
    cfg = load_ws_amazon_cfg()
    log.info(f"引数: {sorted(args) or '(なし)'} / 判定: {cfg['amazon_mall_name']} = {cfg['amazon_status']}")

    state = load_state()
    auto = "--auto" in args
    dry = "--dry-run" in args
    if auto and not state:
        raise SystemExit("自動実行には基準データが必要です。先に手動で一度 python amazon_stock_sync.py を実行してください")
    full = "--full" in args or not state

    if full:
        # Amazon上の実在庫を基準にする（WS連携が止まっていた間のずれもここで解消される）
        state = fetch_amazon_listings(acfg)
    in_stock = collect_amazon_stock(cfg, query=ws.IN_STOCK_QUERY)
    if full and not in_stock:
        raise SystemExit("WSから在庫ありの商品が1件も取れませんでした。全SKUを0にしないよう中止します")
    changed = compute_changed(cfg, state, in_stock)
    log.info(f"送信が必要な変更: {len(changed):,}件")
    if full and not dry and "--validate" not in args:
        save_state(state)  # 基準（Amazon上の実在庫）を保存。送信に失敗した分は次回の差分で再送される
    if not changed:
        log.info("変更なし。送信は不要です。")
        return
    if auto and len(changed) > AUTO_MAX_CHANGES:
        raise SystemExit(f"変更件数が多すぎるため自動送信を中止しました（{len(changed):,}件 > 上限 {AUTO_MAX_CHANGES:,}件）")

    if "--validate" in args:
        run_validate(acfg, changed)
        return
    if dry:
        to0 = [s for s, q in changed.items() if q == 0]
        to1 = [s for s, q in changed.items() if q == 1]
        for s in to0[:10]:
            log.info(f"  送信予定: {s} → 0")
        for s in to1[:10]:
            log.info(f"  送信予定: {s} → 1")
        out = BASE_DIR / "stock_csv" / f"amazon_dry_run_{datetime.now():%Y%m%d_%H%M%S}.tsv"
        out.write_text("SKU\t送信する在庫\n" + "".join(f"{s}\t{q}\n" for s, q in changed.items()), encoding="utf-8-sig")
        log.info(f"dry-run のため送信しません。全件の一覧: {out}")
        return
    if not auto:
        answer = input(f"{len(changed):,}件をAmazonへ送信します。よろしいですか？ (yes と入力): ")
        if answer.strip().lower() != "yes":
            log.info("キャンセルしました。")
            return

    items = list(changed.items())
    failed_files = 0
    for start in range(0, len(items), CHUNK_SIZE):
        part = dict(items[start:start + CHUNK_SIZE])
        try:
            ok, failed = submit_feed(acfg, part)
        except Exception as e:
            failed_files += 1
            log.error(f"フィード送信に失敗（この分は状態を更新しません。次回再送されます）: {e}")
            continue
        state.update({s: part[s] for s in ok})
        save_state(state)
        for s, msg in list(failed.items())[:50]:
            log.error(f"Amazonがエラーを返したSKU: {s} / {msg}")
        if failed:
            log.error(f"エラーSKU: {len(failed):,}件（次回再送されます）")
    if failed_files:
        log.error(f"失敗したフィード: {failed_files}件。次回実行時に差分として再送されます。")
    else:
        log.info("すべてのフィードの送信と状態の更新が完了しました。")


def acquire_lock():
    if LOCK_FILE.exists():
        if time.time() - LOCK_FILE.stat().st_mtime < 2 * 3600:
            log.warning("別の実行が進行中のため、今回は中止します")
            return False
        log.warning("古いロックファイルを無視して続行します")
    LOCK_FILE.write_text(str(datetime.now()), encoding="utf-8")
    return True


if __name__ == "__main__":
    if not acquire_lock():
        sys.exit(0)
    try:
        main()
    except SystemExit as e:
        log.error(str(e))
    except Exception as e:
        log.exception(f"予期しないエラー: {e}")
    finally:
        LOCK_FILE.unlink(missing_ok=True)
        log.info(f"ログ: {ws.log_file}")
