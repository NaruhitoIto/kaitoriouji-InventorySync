# -*- coding: utf-8 -*-
"""
ワサビスイッチ(WS)の在庫を メルカリShops へ API（updateProductVariants）で反映するツール。
在庫は 0 / 1 の二値。前回送信からの差分だけを、SKU（商品管理コード）指定で20件ずつ送る。

初回（または --full）はメルカリShopsの全商品を取得して「メルカリ上の実在庫」を基準にし、WS と照合する。
16万件規模だとレート制限（10,000ポイント/時）を超えることがあるため、残りポイントが少なくなったら
途中経過を mercari_full_scan.json に保存して止まり、次の --full で続きから取得する。
送信状態（mercari_stock_state.json）のキーは「メルカリに存在するSKU」。存在しないSKUには送らない。
WS では「在庫1 かつ メルカリが出品中」だけを在庫1として扱う。

WS の在庫は、wasabi_stock_sync.py が保存する ws_instock_snapshot.json を使う（毎時の取得を二重にしないため）。
wasabi_config.ini の [WASABI] に snapshot_malls = メルカリ を入れると保存されるようになる。
スナップショットが古い・無い場合、手動実行では WS から取得し直す（約25分）。--auto では次回に回す。

使い方:
  python mercari_stock_sync.py --test       接続確認（ショップ情報と商品3件を表示。在庫は送らない）
  python mercari_stock_sync.py --dry-run    差分を計算して一覧を出すだけ（送信しない）
  python mercari_stock_sync.py              通常実行（差分を確認 → 送信）
  python mercari_stock_sync.py --auto       無確認の自動実行（基準データが必要）
  python mercari_stock_sync.py --full       メルカリの全商品と照合して基準を作り直す（--full --dry-run も可）
  python mercari_stock_sync.py --fetch-ws   スナップショットを使わず WS から取得する（他の引数と併用）

WS の設定は wasabi_stock_sync.py と同じ wasabi_config.ini を使う。追加できる項目（[WASABI]）:
  mercari_mall_name = メルカリ  ← WS の「出品状況」に出るメルカリShopsのモール名
  mercari_status    = 出品中
"""
import configparser
import json
import logging
import sys
import time
from datetime import datetime, timedelta

import wasabi_stock_sync as ws
from mercari_shops_api import (BASE_DIR, GraphQLError, RateLimited, gql, load_mercari_config, rate_low,
                               rate_summary)

STATE_FILE = BASE_DIR / "mercari_stock_state.json"
FULL_SCAN_FILE = BASE_DIR / "mercari_full_scan.json"
LOCK_FILE = BASE_DIR / "mercari_stock_sync.lock"
SNAPSHOT_MAX_AGE = timedelta(minutes=90)
FULL_SCAN_MAX_AGE = timedelta(hours=24)  # これより古い途中経過は捨てて最初から取り直す
PRODUCTS_PAGE = 200       # 全件照合の1ページの商品数（API上限。コストは 200 × 子ノード数で、1リクエスト上限2000に収まる）
BATCH = 20                # updateProductVariants の上限
SAVE_EVERY = 25           # 送信状態を保存する間隔（バッチ数）
AUTO_MAX_CHANGES = 20000  # 自動実行で一度に送る上限


def _use_own_log_file():
    """wasabi_stock_sync を読み込むと stock_sync_*.log に書かれるため、メルカリ用のログファイルに付け替える。"""
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, logging.FileHandler) and h.baseFilename == str(ws.log_file):
            root.removeHandler(h)
            h.close()
            if ws.log_file.exists() and ws.log_file.stat().st_size == 0:
                ws.log_file.unlink()
    path = ws.LOG_DIR / f"mercari_stock_{datetime.now():%Y%m%d_%H%M%S}.log"
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    root.addHandler(fh)
    return path


log_file = _use_own_log_file()
log = logging.getLogger("mercari_stock_sync")


def load_ws_mercari_cfg():
    cfg = ws.load_configs()
    wc = configparser.ConfigParser()
    wc.read(ws.WASABI_CONFIG, encoding="utf-8")
    cfg["mercari_mall_name"] = wc.get("WASABI", "mercari_mall_name", fallback="メルカリ").strip()
    cfg["mercari_status"] = wc.get("WASABI", "mercari_status", fallback="出品中").strip()
    return cfg


def is_mercari_listed(item, cfg):
    return ws.listing_status(item, cfg["mercari_mall_name"]) == cfg["mercari_status"]


# ---------- WS 側（在庫1 かつ メルカリ出品中 の SKU） ----------

def in_stock_from_snapshot(cfg):
    """wasabi_stock_sync.py が保存したスナップショットから、在庫1かつメルカリ出品中のSKU（小文字）を返す。
    使えない（無い・古い・メルカリのモールが入っていない）ときは None。"""
    if not ws.SNAPSHOT_FILE.exists():
        log.info("WSのスナップショットがありません")
        return None
    snap = json.loads(ws.SNAPSHOT_FILE.read_text(encoding="utf-8"))
    created = datetime.fromisoformat(snap["created_at"])
    age = datetime.now() - created
    if age > SNAPSHOT_MAX_AGE:
        log.info(f"WSのスナップショットが古いため使いません（{created:%Y-%m-%d %H:%M} 作成）")
        return None
    statuses = (snap.get("malls") or {}).get(cfg["mercari_mall_name"])
    if statuses is None:
        log.info(f"WSのスナップショットに「{cfg['mercari_mall_name']}」がありません（wasabi_config.ini の snapshot_malls を確認）")
        return None
    in_stock = {sku.lower() for sku, st in statuses.items() if st == cfg["mercari_status"]}
    log.info(f"WSのスナップショット（{created:%H:%M} 作成）を使用: 在庫1かつメルカリ出品中 {len(in_stock):,}件")
    return in_stock


def in_stock_from_ws(cfg):
    """WS から在庫1の商品を取得し、メルカリ出品中のSKU（小文字）を返す（全件で約25分）。"""
    in_stock, skipped = set(), 0
    for item in ws.iter_products(cfg, query=ws.IN_STOCK_QUERY):
        if not is_mercari_listed(item, cfg):
            skipped += 1
            continue
        sku = str(item.get("商品番号") or "").strip()
        if sku and ws.parse_stock(item.get("在庫数")):
            in_stock.add(sku.lower())
    log.info(f"WS 在庫1かつメルカリ出品中: {len(in_stock):,}件 / メルカリ出品中以外でスキップ: {skipped:,}件")
    return in_stock


def recheck_in_stock(cfg, skus):
    """「在庫0になった」候補を、送信前に商品番号で WS に問い合わせ直し、まだ在庫1かつメルカリ出品中のSKU（小文字）を返す。"""
    found = set()
    for start in range(0, len(skus), ws.RECHECK_BATCH):
        batch = skus[start:start + ws.RECHECK_BATCH]
        page, got = 1, 0
        while True:
            data = ws.fetch_page(cfg, page, {"商品番号": {"in": batch}})
            items = data.get("data", [])
            for item in items:
                if ws.parse_stock(item.get("在庫数")) and is_mercari_listed(item, cfg):
                    found.add(str(item.get("商品番号") or "").strip().lower())
            got += len(items)
            if not items or got >= (data.get("total") or 0):
                break
            page += 1
            time.sleep(0.4)
        time.sleep(0.4)
    return found


# ---------- メルカリ側 ----------

PRODUCTS_QUERY = """
query products($first: Int, $after: String) {
  products(first: $first, after: $after) {
    pageInfo { hasNextPage endCursor }
    edges { node { variants { skuCode stockQuantity } } }
  }
}
"""

UPDATE_MANY = """
mutation updateProductVariants($inputs: [UpdateProductVariantsInput!]!) {
  updateProductVariants(inputs: $inputs) { productVariants { skuCode stockQuantity } }
}
"""

UPDATE_ONE = """
mutation updateProductVariant($by: ProductVariantBy!, $input: UpdateProductVariantInput!) {
  updateProductVariant(by: $by, input: $input) { productVariant { skuCode stockQuantity } }
}
"""


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_json(path, data):
    text = json.dumps(data, ensure_ascii=False)
    for attempt in range(6):
        try:
            path.write_text(text, encoding="utf-8")
            return
        except PermissionError:
            log.warning(f"{path.name} を書き込めません。再試行 {attempt + 1}/6")
            time.sleep(5)
    raise RuntimeError(f"{path.name} を保存できませんでした")


def fetch_mercari_listings(mcfg):
    """メルカリShopsの全商品を取得し {SKU(メルカリの表記): 在庫(0/1)} を返す。
    レート制限で最後まで取れなかったときは途中経過を保存して None を返す（次の --full で続きから）。"""
    scan = load_json(FULL_SCAN_FILE)
    if scan and datetime.now() - datetime.fromisoformat(scan["started_at"]) > FULL_SCAN_MAX_AGE:
        log.info("全件照合の途中経過が古いため、最初から取り直します")
        scan = {}
    if scan:
        log.info(f"全件照合を続きから再開します（{scan['started_at'][:16]} 開始 / 取得済み {len(scan['listings']):,}件）")
    else:
        scan = {"started_at": datetime.now().isoformat(), "cursor": None, "listings": {}, "pages": 0, "no_sku": 0}

    while True:
        if rate_low(mcfg):
            save_json(FULL_SCAN_FILE, scan)
            log.warning(f"残りポイントが少ないため全件照合を中断しました（取得済み {len(scan['listings']):,}件）。"
                        f"{rate_summary()}。1時間ほどあけて、もう一度 --full を実行すると続きから取得します")
            return None
        try:
            data = gql(mcfg, PRODUCTS_QUERY, {"first": PRODUCTS_PAGE, "after": scan["cursor"]})
        except RateLimited as e:
            save_json(FULL_SCAN_FILE, scan)
            log.warning(f"{e}。全件照合を中断しました（取得済み {len(scan['listings']):,}件）。次の --full で続きから取得します")
            return None
        conn = data.get("products") or {}
        for edge in conn.get("edges") or []:
            for v in (edge.get("node") or {}).get("variants") or []:
                sku = (v.get("skuCode") or "").strip()
                if not sku:
                    scan["no_sku"] += 1
                    continue
                scan["listings"][sku] = 1 if (v.get("stockQuantity") or 0) > 0 else 0
        scan["pages"] += 1
        page = conn.get("pageInfo") or {}
        scan["cursor"] = page.get("endCursor")
        if scan["pages"] % 20 == 0:
            log.info(f"メルカリ商品を取得中: {scan['pages']}ページ / SKU {len(scan['listings']):,}件 / {rate_summary()}")
            save_json(FULL_SCAN_FILE, scan)
        if not page.get("hasNextPage"):
            break

    listings = scan["listings"]
    log.info(f"メルカリShopsの全SKU: {len(listings):,}件（うち在庫あり {sum(listings.values()):,}件）/ "
             f"SKU未設定で除外: {scan['no_sku']:,}件 / {rate_summary()}")
    FULL_SCAN_FILE.unlink(missing_ok=True)
    return listings


def send_one(mcfg, sku, qty):
    """1SKUだけ送る。戻り値は None（成功）か エラーコード一覧。並行更新の競合は1回だけ再試行する。"""
    for attempt in range(2):
        try:
            gql(mcfg, UPDATE_ONE, {"by": {"skuCode": sku}, "input": {"stockQuantity": qty}})
            return None
        except GraphQLError as e:
            if "PRODUCT_DIFFERENCE_FOUND" in e.codes and attempt == 0:
                time.sleep(1)
                continue
            return e.codes or ["UNKNOWN"]
    return ["PRODUCT_DIFFERENCE_FOUND"]


def send_updates(mcfg, state, changed):
    """変更を20件ずつ送る。成功分だけ state を更新して保存する。
    バッチがエラーになったら1件ずつ送り直して原因のSKUを特定する。存在しない商品は state から外す。
    戻り値は (成功件数, 失敗 {sku: エラーコード}, 未送信件数)。"""
    items = list(changed.items())
    ok, failed, removed = 0, {}, []
    sent_batches = 0
    pos = 0
    try:
        while pos < len(items):
            if rate_low(mcfg):
                log.warning(f"残りポイントが少ないため送信を中断します。{rate_summary()}")
                break
            batch = items[pos:pos + BATCH]
            inputs = [{"by": {"skuCode": s}, "input": {"stockQuantity": q}} for s, q in batch]
            try:
                gql(mcfg, UPDATE_MANY, {"inputs": inputs})
                for s, q in batch:
                    state[s] = q
                ok += len(batch)
            except GraphQLError as e:
                log.warning(f"一括更新がエラーのため1件ずつ送り直します: {e}")
                for s, q in batch:
                    codes = send_one(mcfg, s, q)
                    if codes is None:
                        state[s] = q
                        ok += 1
                    elif "NOT_FOUND" in codes:
                        state.pop(s, None)  # メルカリから削除された商品。以後は送らない
                        removed.append(s)
                    else:
                        failed[s] = ",".join(codes)
            pos += len(batch)
            sent_batches += 1
            if sent_batches % SAVE_EVERY == 0:
                save_json(STATE_FILE, state)
                log.info(f"送信中: {pos:,}/{len(items):,}件 / {rate_summary()}")
    except RateLimited as e:
        log.warning(f"{e}。残りは次回送ります")
    finally:
        save_json(STATE_FILE, state)
    if removed:
        log.warning(f"メルカリに存在しないため送信対象から外したSKU: {len(removed):,}件（例: {', '.join(removed[:10])}）")
    return ok, failed, len(items) - pos


def compute_changed(cfg, state, in_stock, recheck=True):
    """state（メルカリ上の在庫）と WS の「在庫1かつメルカリ出品中」（小文字）を比べ、送るべき {SKU: 0/1} を返す。
    メルカリに無いSKU（state に無いもの）には送らない。SKU はメルカリの表記のまま送る。"""
    changed = {s: 1 for s, q in state.items() if q != 1 and s.lower() in in_stock}
    dropped = [s for s, q in state.items() if q == 1 and s.lower() not in in_stock]
    if dropped and recheck:
        # 取得中のずれなどで一覧から漏れただけの商品に0を送らないよう、送信前に商品番号で確かめ直す
        log.info(f"在庫0にする候補 {len(dropped):,}件を商品番号で再確認します")
        still = recheck_in_stock(cfg, dropped)
        if still:
            log.warning(f"再確認で在庫あり・メルカリ出品中だったため0送信から外しました: {len(still):,}件")
            dropped = [s for s in dropped if s.lower() not in still]
    for s in dropped:
        changed[s] = 0
    on_mercari = {s.lower() for s in state}
    not_on_mercari = sum(1 for s in in_stock if s not in on_mercari)
    log.info(f"在庫1にする: {len(changed) - len(dropped):,}件 / 在庫0にする: {len(dropped):,}件 / "
             f"WSは在庫1だがメルカリに商品が無い（送らない）: {not_on_mercari:,}件")
    return changed


def run_test(mcfg):
    shop = gql(mcfg, "query { shop { id name shopStatus enableMultiplePurchase } }").get("shop") or {}
    log.info(f"接続OK（{mcfg['environment']}）。ショップ: {shop.get('name')} / 状態: {shop.get('shopStatus')} / "
             f"複数購入: {shop.get('enableMultiplePurchase')}")
    data = gql(mcfg, PRODUCTS_QUERY, {"first": 3, "after": None})
    for edge in (data.get("products") or {}).get("edges") or []:
        for v in (edge.get("node") or {}).get("variants") or []:
            log.info(f"  商品例: SKU={v.get('skuCode')} / 在庫={v.get('stockQuantity')}")
    log.info(rate_summary())


def main():
    args = set(sys.argv[1:])
    mcfg = load_mercari_config()
    if "--test" in args:
        run_test(mcfg)
        return
    cfg = load_ws_mercari_cfg()
    log.info(f"引数: {sorted(args) or '(なし)'} / 環境: {mcfg['environment']} / "
             f"判定: {cfg['mercari_mall_name']} = {cfg['mercari_status']}")

    state = load_json(STATE_FILE)
    auto = "--auto" in args
    dry = "--dry-run" in args
    if auto and not state:
        raise SystemExit("自動実行には基準データが必要です。先に手動で一度 python mercari_stock_sync.py --full を実行してください")
    full = "--full" in args or not state

    in_stock = None if "--fetch-ws" in args else in_stock_from_snapshot(cfg)
    if in_stock is None:
        if auto:
            log.info("自動実行ではWSの再取得（約25分）をしないため、今回は送信しません（次回に回します）")
            return
        in_stock = in_stock_from_ws(cfg)

    if full:
        # メルカリ上の実在庫を基準にする（WS連携が止まっていた間のずれもここで解消される）
        listings = fetch_mercari_listings(mcfg)
        if listings is None:
            return  # レート制限で途中まで。続きは次の --full で
        if not in_stock:
            raise SystemExit("WSから在庫ありの商品が1件も取れませんでした。全SKUを0にしないよう中止します")
        state = listings
        if not dry:
            save_json(STATE_FILE, state)  # 基準（メルカリ上の実在庫）を保存。送信に失敗した分は次回の差分で再送される

    changed = compute_changed(cfg, state, in_stock)
    log.info(f"送信が必要な変更: {len(changed):,}件")
    if not changed:
        log.info("変更なし。送信は不要です。")
        return
    if auto and len(changed) > AUTO_MAX_CHANGES:
        raise SystemExit(f"変更件数が多すぎるため自動送信を中止しました（{len(changed):,}件 > 上限 {AUTO_MAX_CHANGES:,}件）")

    if dry:
        for s, q in list(changed.items())[:20]:
            log.info(f"  送信予定: {s} → {q}")
        out = ws.OUT_DIR / f"mercari_dry_run_{datetime.now():%Y%m%d_%H%M%S}.tsv"
        out.write_text("SKU\t送信する在庫\n" + "".join(f"{s}\t{q}\n" for s, q in changed.items()), encoding="utf-8-sig")
        log.info(f"dry-run のため送信しません。全件の一覧: {out}")
        return
    if not auto:
        answer = input(f"{len(changed):,}件をメルカリShops（{mcfg['environment']}）へ送信します。よろしいですか？ (yes と入力): ")
        if answer.strip().lower() != "yes":
            log.info("キャンセルしました。")
            return

    ok, failed, unsent = send_updates(mcfg, state, changed)
    for s, codes in list(failed.items())[:50]:
        log.error(f"メルカリがエラーを返したSKU: {s} / {codes}")
    log.info(f"送信完了: 成功 {ok:,}件 / エラー {len(failed):,}件 / 未送信（次回へ） {unsent:,}件 / {rate_summary()}")
    if failed or unsent:
        log.error("エラー・未送信の分は、次回実行時に差分として再送されます。")
    else:
        log.info("すべての送信と状態の更新が完了しました。")


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
        log.info(f"ログ: {log_file}")
