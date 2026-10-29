# -*- coding: utf-8 -*-
"""
ワサビスイッチ(WS)停止中の代替: WS を通さず、モール間で直接「売れた商品の在庫を0」にする。

  楽天の注文（RMS API）              → Amazon・ヤフーのストアの在庫を0（Listings API / 在庫更新API）
  Amazonの注文（SP-API）             → 楽天・ヤフーのストアの在庫を0（RMS 在庫CSV を SFTP / 在庫更新API）
  メルカリ・ヤフオクの注文（手動ファイル）→ 楽天・Amazon・ヤフーのストアの在庫を0
  ヤフーショッピングの注文（注文API、承認されるまでは手動ファイル）→ ワサビ・楽天・Amazon の在庫を0
  ヤフオク店（ストアオークション）の注文（同じ注文API。商品名から商品番号を探す）→ ワサビ・楽天・Amazon・ヤフーのストアの在庫を0
  どこかで売れたSKU                  → メルカリ・ヤフオクで手動で止める一覧（todo/）を作る
  ワサビで在庫0になったSKU（毎時の wasabi_stock_sync が ws_zero_queue.txt に書く）→ ヤフーのストアの在庫を0

在庫は「0にする」方向しか送らない（売れた1点物を止めるだけ）。キャンセル・返品で戻すのは手動。
WS の状態ファイル（wasabi_stock_state.json）は読むだけで変更しない（WS 復旧後の処理を壊さないため）。
ワサビの在庫は、ヤフーショッピングで売れた分だけ FTP（/stock_import）で0にする（ワサビ自身のヤフーの注文の
取り込みが1〜2日遅れるため。ワサビからメルカリ・ラクマなどにも伝わる）。ワサビが復旧した後も、この分のために動かしておく。

手動ファイルの置き場所（処理後は done/ に移動）:
  inbox/mercari/  メルカリShopsの注文CSV、またはSKUを1行に1つ書いたテキスト
  inbox/yahoo/    ヤフオクの注文（落札）CSV、またはSKUを1行に1つ書いたテキスト
  inbox/yahoo_shopping/  ストアクリエイターProの注文データの明細（default_all_items.csv）。注文（default_all_orders.csv）も
                  一緒に置くとキャンセルを除く。ストアオークション（AUCTION…）の明細は使わない
  CSV は見出しに「商品管理コード / 商品番号 / SKU / 管理番号」などがあればその列を、無ければ
  すべての欄から商品番号の形（例 260908SK040605）を探して読む。注文者情報は読まない・記録しない。

使い方:
  python cross_mall_sync.py --since 2026-10-21T23:00:00+09:00 --dry-run   初回: 開始時刻を指定して確認（送信しない）
  python cross_mall_sync.py --since 2026-10-21T23:00:00+09:00             初回: 送信する
  python cross_mall_sync.py --dry-run                                     2回目以降の確認
  python cross_mall_sync.py                                               2回目以降（定期実行もこれ）
  python cross_mall_sync.py --validate    Amazon は検証モード（在庫は変わらない）、楽天は送らない
  python cross_mall_sync.py --ws-export   WS 復旧時に取り込ませる「売れたSKU」のCSVを作る（送信はしない）
  python cross_mall_sync.py --mercari-upload   メルカリの在庫一括更新CSVだけを作る（注文は取得しない）

メルカリShopsの在庫一括更新ファイル:
  inbox/mercari_products/ にメルカリShopsの商品CSV（一括編集用ダウンロード）を置くと、毎回の実行で
  todo/メルカリ_在庫更新_アップロード_*.csv（商品ID, SKU1_ID, SKU1_現在の在庫数, SKU1_増減フラグ=2, SKU1_在庫増減数）を作る。
  一度ファイルに出したSKUは次から出さない（同じ商品を二重に減らさないため）。商品CSVは複数ファイルに分けてよく、追加してよい。
  商品CSV（在庫ありリスト）があるときは、それをメルカリの出品一覧とみなし、手動で止める一覧（todo/メルカリ_止める_*）は作らない。
  リストに無いSKUはメルカリに出品が無いものとして扱う（参考に todo/参考/メルカリ_商品CSVに無い.csv を出す）。
  在庫更新ファイルは5項目（商品ID, SKU1_ID, SKU1_現在の在庫数, SKU1_増減フラグ, SKU1_在庫増減数）。アップロードしたら
  todo/Uploaded/ へ移す。todo/ 直下に未アップロードのファイルが残っていれば、新しい分と1つのファイルに結合する。

ヤフオク: 在庫連携をやめたため、止める一覧は作らない（inbox/yahoo/ に注文ファイルを置けば、楽天・Amazon の在庫0はする）。
"""
import configparser
import csv
import ftplib
import io
import json
import logging
import re
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import rakuten_rms_api as rms
import wasabi_stock_sync as ws
from amazon_sp_api import BASE_DIR, load_amazon_config, sp_request

JST = timezone(timedelta(hours=9))
STATE_FILE = BASE_DIR / "cross_mall_state.json"
LOCK_FILE = BASE_DIR / "cross_mall_sync.lock"
INBOX = BASE_DIR / "inbox"
TODO_DIR = BASE_DIR / "todo"
RAKUTEN_CFG = BASE_DIR / "rakuten_api_config.ini"
OVERLAP = timedelta(minutes=10)      # 取得期間を前回と少し重ねる（注文番号で重複は除く）
AMAZON_ACTIVE = "Pending,Unshipped,PartiallyShipped,Shipped"
RAKUTEN_CANCEL = {"700", "800", "900"}
AUTO_TARGETS = ("rakuten", "amazon", "yahoo_store", "wasabi")  # API・FTP で自動的に0にする送り先
# 送り先ごとに「この売れたモールなら送らない」（ヤフーのストアで売れた分はヤフー側で在庫が減る）
SKIP_SOURCES = {"yahoo_store": {"yahoo"}}
# 送り先ごとに「この売れたモールの分だけ送る」。ワサビは楽天の注文（rakuten_orders_sync）や Amazon・メルカリの注文
# （ワサビ自身が取り込む）をすでに知っているため、取り込みが1〜2日遅れるヤフーショッピングの分だけ送る
ONLY_SOURCES = {"wasabi": {"yahoo_store", "yahoo_auction"}}
MANUAL_MALLS = {"mercari": "メルカリ", "yahoo": "ヤフオク"}  # 手動で止めるモール（一覧を作る）
MALL_LABEL = {"rakuten": "楽天", "amazon": "Amazon", "yahoo_store": "ヤフーショッピング", "yahoo_auction": "ヤフオク店",
              "wasabi": "ワサビ", **MANUAL_MALLS}
WASABI_FTP_CFG = BASE_DIR / "wasabi_ftp_config.ini"
YAHOO_SHOPPING_INBOX = INBOX / "yahoo_shopping"  # ヤフーショッピングの明細ファイル（default_all_items.csv）を置く
YAHOO_LOOKBACK = timedelta(days=2)  # 注文APIが使えるようになった最初の回などに、さかのぼって注文を取る上限
AUCTION_BACKFILL = timedelta(days=3)  # ストアオークションに対応する前に処理した注文を、最初の1回だけ見直す期間
MALLS_LIKE_RAKUTEN = {"mercari"}  # 楽天と同じ商品を出品している想定のモール（止める一覧を楽天の出品で絞る）
TODO_STOPPED = {"yahoo", "mercari"}  # 止める一覧・アップロード用ファイルを作らないモール
# （ヤフオクは在庫連携をやめた。メルカリはワサビの連携が再開した。注文ファイルを置けば他モールの在庫0はする）
MERCARI_UPLOAD = False  # メルカリの在庫更新アップロード用ファイルを作るか（ワサビ停止中だけ True にする）
MAX_RETRY = 5
SKU_RE = re.compile(r"(?<![0-9A-Za-z])(\d{6}(?:[A-Za-z]{2}\d{6}|[A-Za-z]\d{7}))(?![0-9A-Za-z])")
SKU_COLUMNS = ("商品管理コード", "商品番号", "SKU", "sku", "SKUコード", "管理番号", "商品管理番号", "商品コード",
               "original_product_id", "出品者SKU", "YahooAuctionMerchantId")
ORDER_COLUMNS = ("注文番号", "注文ID", "取引ID", "order_id", "OrderId", "注文No", "オークションID", "YahooAuctionId")
DATE_COLUMNS = ("OrderTime", "purchase_date", "注文日時", "購入日時", "落札日時")
DATE_FORMATS = ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                "%Y年%m月%d日 %H:%M:%S", "%Y年%m月%d日 %H:%M")


def parse_order_time(s):
    s = (s or "").strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=JST)
        except ValueError:
            continue
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=JST)
    except ValueError:
        return None


def _use_own_log_file():
    """wasabi_stock_sync を読み込むと stock_sync_*.log に書かれるため、このスクリプト用のログに付け替える。"""
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, logging.FileHandler) and h.baseFilename == str(ws.log_file):
            root.removeHandler(h)
            h.close()
            if ws.log_file.exists() and ws.log_file.stat().st_size == 0:
                ws.log_file.unlink()
    path = ws.LOG_DIR / f"cross_mall_{datetime.now():%Y%m%d_%H%M%S}.log"
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    root.addHandler(fh)
    return path


log_file = _use_own_log_file()
log = logging.getLogger("cross_mall_sync")


def norm(sku):
    return (sku or "").strip().upper()


# ---------- 状態 ----------

def load_state(since):
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    if not since:
        raise SystemExit("初回は --since で開始時刻を指定してください（例 --since 2026-10-21T23:00:00+09:00）")
    return {"since": since.isoformat(),
            "rakuten": {"last_checked": since.isoformat(), "processed": []},
            "amazon": {"last_checked": since.isoformat(), "processed": []},
            "ledger": {}}


def save_state(state):
    data = json.dumps(state, ensure_ascii=False, indent=1)
    for attempt in range(6):
        try:
            tmp = STATE_FILE.with_suffix(".tmp")
            tmp.write_text(data, encoding="utf-8")
            tmp.replace(STATE_FILE)
            return
        except PermissionError:
            log.warning(f"状態ファイルを書き込めません。再試行 {attempt + 1}/6")
            time.sleep(5)
    raise RuntimeError("cross_mall_state.json を保存できませんでした")


def targets_for(src):
    """そのモールで売れたSKUを在庫0にする送り先。"""
    return [t for t in AUTO_TARGETS
            if t != src and src not in SKIP_SOURCES.get(t, ()) and src in ONLY_SOURCES.get(t, {src})]


def add_sold(state, sku, src, order_id, at):
    """売れたSKUを台帳に追加する。すでにあれば何もしない（最初に売れたモールを残す）。"""
    key = norm(sku)
    if not key:
        return False
    if key in state["ledger"]:
        e = state["ledger"][key]
        if e["src"] != src and src not in e.setdefault("also", {}):
            # 別のモールでも売れている = 二重販売。どちらかの注文をキャンセルする必要がある
            e["also"][src] = order_id
            log.warning(f"二重販売の可能性: {key} が {MALL_LABEL[e['src']]}（{e['order']}）と "
                        f"{MALL_LABEL[src]}（{order_id}）で売れています")
        return False
    entry = {"src": src, "order": order_id, "at": at, "push": {}, "tries": {}, "todo": {}}
    for t in targets_for(src):
        entry["push"][t] = None  # None = 未送信
    for m in MANUAL_MALLS:
        if m != src:
            entry["todo"][m] = None  # None = まだ一覧に出していない
    state["ledger"][key] = entry
    return True


# ---------- 楽天（注文取得） ----------

def rakuten_orders(start, end):
    """注文日時が期間内の注文を取得し、[(注文番号, 進捗, [SKU...])] を返す。"""
    keys = rms.load_keys(RAKUTEN_CFG)
    if not keys:
        raise RuntimeError("rakuten_api_config.ini の [RAKUTEN_API] service_secret / license_key がありません")
    result = []
    for o in rms.get_orders(keys, rms.search(keys, start, end), version=6):
        skus = [it.get("itemNumber") or it.get("manageNumber")
                for pkg in o.get("PackageModelList", []) for it in pkg.get("ItemModelList", [])]
        result.append((o.get("orderNumber"), str(o.get("orderProgress", "")), [s for s in skus if s]))
    return result


def collect_rakuten(state, now, dry):
    st = state["rakuten"]
    start = datetime.fromisoformat(st["last_checked"]) - OVERLAP
    floor = datetime.fromisoformat(state["since"])
    orders = rakuten_orders(max(start, floor), now)
    processed, new, added = set(st["processed"]), [], 0
    for num, progress, skus in orders:
        if not num or num in processed:
            continue
        if progress in RAKUTEN_CANCEL:
            continue
        for s in skus:
            added += add_sold(state, s, "rakuten", num, now.isoformat())
        new.append(num)
    st["processed"] = (st["processed"] + new)[-20000:]
    st["last_checked"] = now.isoformat()
    log.info(f"楽天の注文: 取得 {len(orders)}件 / 新規 {len(new)}件 / 新たに売れたSKU {added}件")


# ---------- Amazon（注文取得・在庫0） ----------

def iso_utc(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def collect_amazon(state, acfg, now):
    st = state["amazon"]
    start = datetime.fromisoformat(st["last_checked"]) - OVERLAP
    floor = datetime.fromisoformat(state["since"])
    before = now - timedelta(minutes=3)  # SP-API は直近2分より前を指定する必要がある
    params = {"MarketplaceIds": acfg["marketplace_id"], "LastUpdatedAfter": iso_utc(max(start, floor)),
              "LastUpdatedBefore": iso_utc(before), "OrderStatuses": AMAZON_ACTIVE, "MaxResultsPerPage": 100}
    orders = []
    while True:
        j = sp_request(acfg, "GET", "/orders/v0/orders", params=params)
        payload = j.get("payload", {})
        orders.extend(payload.get("Orders", []))
        token = payload.get("NextToken")
        if not token:
            break
        params = {"MarketplaceIds": acfg["marketplace_id"], "NextToken": token}
        time.sleep(1)
    processed, new, added = set(st["processed"]), [], 0
    for o in orders:
        oid = o.get("AmazonOrderId")
        if not oid or oid in processed:
            continue
        # 開始時刻より前に購入された注文（後から更新されただけのもの）は対象外
        if datetime.fromisoformat(o["PurchaseDate"].replace("Z", "+00:00")) < floor:
            continue
        items, ip = [], None
        while True:
            jj = sp_request(acfg, "GET", f"/orders/v0/orders/{oid}/orderItems", params=ip)
            pl = jj.get("payload", {})
            items.extend(i["SellerSKU"] for i in pl.get("OrderItems", []) if i.get("SellerSKU"))
            if not pl.get("NextToken"):
                break
            ip = {"NextToken": pl["NextToken"]}
            time.sleep(2)
        for s in items:
            added += add_sold(state, s, "amazon", oid, now.isoformat())
        new.append(oid)
        time.sleep(2)  # getOrderItems は約0.5リクエスト/秒
    st["processed"] = (st["processed"] + new)[-20000:]
    st["last_checked"] = before.isoformat()
    log.info(f"Amazonの注文: 取得 {len(orders)}件 / 新規 {len(new)}件 / 新たに売れたSKU {added}件")


def amazon_zero(acfg, sku, validate):
    """Amazon の在庫を0にする。戻り値 'ok' / 'absent'（Amazonに出品なし）/ 'fba'（FBAのみで対象外）/ 'error:...'
    PATCH は存在しないSKUでも受け付けてしまうため、先に出品を取得して、自己発送の出品があるときだけ送る。"""
    path = f"/listings/2021-08-01/items/{acfg['seller_id']}/{quote(sku, safe='')}"
    try:
        li = sp_request(acfg, "GET", path, params={"marketplaceIds": acfg["marketplace_id"],
                                                   "includedData": "summaries,fulfillmentAvailability"})
    except RuntimeError as e:
        return "absent" if "HTTP 404" in str(e) else "error:" + str(e)[:200]
    fa = li.get("fulfillmentAvailability") or []
    mfn = [f for f in fa if f.get("fulfillmentChannelCode") == "DEFAULT"]
    if fa and not mfn:
        return "fba"
    if mfn and all((f.get("quantity") or 0) == 0 for f in mfn):
        return "ok"  # すでに0
    ptype = next((s.get("productType") for s in li.get("summaries") or [] if s.get("productType")), "PRODUCT")
    if ptype.startswith("ABIS_"):
        ptype = "PRODUCT"  # 旧形式の商品タイプ（ABIS_VIDEO など）は PATCH で指定できないため汎用の PRODUCT で送る
    params = {"marketplaceIds": acfg["marketplace_id"], "issueLocale": "ja_JP"}
    if validate:
        params["mode"] = "VALIDATION_PREVIEW"
    for attempt_type in dict.fromkeys([ptype, "PRODUCT"]):
        body = {"productType": attempt_type, "patches": [{
            "op": "replace", "path": "/attributes/fulfillment_availability",
            "value": [{"fulfillment_channel_code": "DEFAULT", "quantity": 0}]}]}
        try:
            j = sp_request(acfg, "PATCH", path, params=params, json_body=body)
        except RuntimeError as e:
            msg = str(e)
            if "HTTP 404" in msg:
                return "absent"
            return "error:" + msg[:200]
        if j.get("status") == "ACCEPTED" or (validate and j.get("status") == "VALID"):
            return "ok"
        issues = [f"{i.get('code')}: {i.get('message')}" for i in j.get("issues", []) if i.get("severity") == "ERROR"]
        if not any(str(i).startswith("4000003") for i in issues):
            break  # 商品タイプ以外のエラーは再試行しない
    return "error:" + (j.get("status") or "?") + " " + "; ".join(issues)[:200]


# ---------- 手動ファイル（メルカリ・ヤフオク） ----------

def read_text_any(path):
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp932"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("cp932", errors="replace")


def parse_manual_file(path, since):
    """[(SKU, 注文番号)] を返す。見出しにSKU列があればそれを、無ければ全セルから商品番号の形を探す。
    注文日時の列があれば、開始時刻（since）より前の注文は除く。"""
    text = read_text_any(path)
    rows = list(csv.reader(io.StringIO(text), delimiter="\t" if "\t" in text.split("\n", 1)[0] else ","))
    if not rows:
        return []
    header = [h.strip() for h in rows[0]]
    sku_col = next((header.index(c) for c in SKU_COLUMNS if c in header), None)
    order_col = next((header.index(c) for c in ORDER_COLUMNS if c in header), None)
    date_col = next((header.index(c) for c in DATE_COLUMNS if c in header), None)
    if date_col is not None:
        kept, old, unknown = [rows[0]], 0, 0
        for row in rows[1:]:
            t = parse_order_time(row[date_col]) if len(row) > date_col else None
            if t is None:
                unknown += 1
                kept.append(row)  # 日時が読めない行は念のため対象に含める
            elif t < since:
                old += 1
            else:
                kept.append(row)
        log.info(f"{path.name}: 列「{header[date_col]}」で {since:%m/%d %H:%M} より前の注文 {old}件を除外"
                 f"（日時が読めず含めた行: {unknown}件）")
        rows = kept
    found = []
    if sku_col is not None:
        empty = 0
        for row in rows[1:]:
            if len(row) <= sku_col or not row[sku_col].strip():
                empty += 1
                continue
            order = row[order_col].strip() if order_col is not None and len(row) > order_col else ""
            m = SKU_RE.search(row[sku_col])
            found.append((m.group(1) if m else row[sku_col].strip(), order))
        log.info(f"{path.name}: 列「{header[sku_col]}」からSKUを読みました（{len(found)}件 / SKUが空の行: {empty}件）")
        if empty:
            log.warning(f"{path.name}: SKUが空の注文が {empty}件あります。手動で確認してください")
    else:
        for row in rows:
            order = row[order_col].strip() if order_col is not None and len(row) > order_col else ""
            for cell in row:
                found.extend((m, order) for m in SKU_RE.findall(cell))
        log.info(f"{path.name}: SKU列が見つからないため、商品番号の形の文字を探しました（{len(found)}件）")
    return found


def collect_manual(state, now, dry):
    for mall in MANUAL_MALLS:
        folder = INBOX / mall
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "done").mkdir(exist_ok=True)
        for path in sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in (".csv", ".txt", ".tsv")):
            pairs = parse_manual_file(path, datetime.fromisoformat(state["since"]))
            added = sum(add_sold(state, s, mall, o or path.name, now.isoformat()) for s, o in pairs)
            log.info(f"{MALL_LABEL[mall]}の手動ファイル {path.name}: SKU {len(pairs)}件 / 新たに売れたSKU {added}件")
            if not dry:
                shutil.move(str(path), str(folder / "done" / f"{now:%Y%m%d_%H%M%S}_{path.name}"))


# ---------- ヤフーショッピング（注文取得） ----------

def yahoo_sku(code):
    """ヤフーショッピングの商品コードから商品番号を取り出す。ストアオークション（AUCTION…）や形の違うものは ""。"""
    if code.upper().startswith("AUCTION"):
        return ""
    m = SKU_RE.search(code)
    return m.group(1) if m else ""


AUCTION_CODE = re.compile(r"^([A-Za-z]?\d{3}-(\d{4})(?:-\d{2})?)(?=\s)")  # タイトル先頭の管理番号（例 m791-0260-32、336-0494）


def auction_skus(wcfg, title):
    """ストアオークション（ヤフオク店）の明細の商品名から、売れた商品番号を探す。戻り値は (商品番号のリスト, 理由)。
    商品名に商品番号が入っていればそれを使う。無ければワサビの「ヤフー｜商品名」か「表示用商品名」（本・CDは先頭に
    【中古】が付く）が同じ商品、それも無ければ先頭の管理番号で始まり、商品番号の末尾4桁が管理番号の真ん中の4桁と同じ商品。
    1件ならそれ。同じ本が何冊もあるときは、在庫が残っているのが1件ならそれ、全部在庫0ならすでに止まっているので []。
    見つからない・在庫ありが2件以上で決められないときは None。"""
    skus = list(dict.fromkeys(s.upper() for s in SKU_RE.findall(title)))
    if skus:
        return skus, "商品名の商品番号"
    title = title.strip()
    if not title:
        return None, "商品名なし"
    m = AUCTION_CODE.match(title)
    tries = [(f, "includes", title, lambda it, f=f: re.sub(r"^【中古】\s*", "", str(it.get(f) or "").strip()) == title)
             for f in ("ヤフー｜商品名", "表示用商品名")]
    if m:
        tries += [(f, "starts", m.group(1), lambda it: str(it.get("商品番号") or "").endswith(m.group(2)))
                  for f in ("ヤフー｜商品名", "表示用商品名")]
    same, data, items = [], {}, []
    for field, op, value, ok in tries:
        data = ws.fetch_page(wcfg, 1, {field: {op: value}})
        time.sleep(0.4)
        items = data.get("data", [])
        same = [it for it in items if ok(it)]
        if same:
            break
    if not same:
        return None, "ワサビに同じ商品名なし"
    if len(same) == 1:
        return [norm(str(same[0].get("商品番号") or ""))], "ワサビの商品名"
    if (data.get("total") or 0) > len(items):
        return None, f"ワサビに同じ商品名が多すぎる（{data.get('total')}件）"
    in_stock = [norm(str(it.get("商品番号") or "")) for it in same if ws.parse_stock(it.get("在庫数")) != 0]
    if len(in_stock) <= 1:
        return in_stock, f"ワサビに同じ商品名{len(same)}件・在庫あり{len(in_stock)}件"
    return None, f"ワサビに同じ商品名で在庫ありが{len(in_stock)}件（{', '.join(in_stock[:5])}）"


def auction_line_skus(wcfg, title, merchant):
    """ストアオークションの明細の商品番号。注文APIの「ヤフオクの商品コード」（出品時に付けた商品コード）が商品番号の形なら
    それを使い、無ければ商品名から探す（auction_skus）。戻り値は auction_skus と同じ (商品番号のリスト, 理由)。"""
    m = SKU_RE.fullmatch(merchant.strip())
    if m:
        return [m.group(1).upper()], "ヤフオクの商品コード"
    return auction_skus(wcfg, title)


def collect_yahoo_shopping(state, now, wcfg):
    """ヤフーショッピングの注文を注文API（注文検索・注文詳細）で取る。
    注文APIがまだ使えない（利用申請の承認待ち）ときは何もせず、使えるようになったら自動で始まる。
    同じ注文管理に入るストアオークション（ヤフオク店）の明細は、商品コードが AUCTION… で商品番号が無いため、
    注文詳細の「ヤフオクの商品コード」（無ければ商品名から探す。auction_line_skus）で他のモールでも在庫0にする。対応前に処理した注文は、
    最初の1回だけ AUCTION_BACKFILL さかのぼってストアオークションの明細を見直す。"""
    st = state.setdefault("yahoo_store", {"last_checked": now.isoformat(), "processed": []})
    start = max(datetime.fromisoformat(st["last_checked"]) - OVERLAP, now - YAHOO_LOOKBACK)
    backfill = "auction_from" not in st
    if backfill:
        start = min(start, now - AUCTION_BACKFILL)
    try:
        import yahoo_shopping_api as yapi
        ycfg = yapi.load_config()
        orders = yapi.order_list(ycfg, start, now)
    except SystemExit as e:  # 設定ファイルが無いなど
        log.warning(f"ヤフーショッピングの注文: 設定を読めないため取得しません（{e}）")
        return
    except Exception as e:
        if getattr(e, "code", "") == "px-04306":
            log.info("ヤフーショッピングの注文: 注文APIがまだ使えないため取得しません（px-04306。利用申請の承認待ち）")
        else:
            log.error(f"ヤフーショッピングの注文: 取得に失敗しました（次回再試行）: {e}")
        return
    processed, new, added, cancelled, odd, complete = set(st["processed"]), [], 0, 0, [], True
    auction, auction_added, auction_stopped, unmatched = 0, 0, 0, []
    for oid, _, status in orders:
        redo = oid in processed  # 対応前に処理した注文は、ストアオークションの明細だけを見直す
        if not oid or (redo and not backfill):
            continue
        items, found = [], []
        try:
            if status != "4":  # キャンセル以外は注文詳細で商品コードを見る
                time.sleep(1)  # 1クエリー/秒
                status, items = yapi.order_items(ycfg, oid)
            if status != "4":
                for code, _, _, title, merchant in items:
                    is_auction = code.upper().startswith("AUCTION")
                    if is_auction or not redo:
                        found.append((code, title, auction_line_skus(wcfg, title, merchant) if is_auction else None))
        except Exception as e:
            log.error(f"ヤフーショッピングの注文 {oid}: 明細を確かめられませんでした（次回再試行）: {e}")
            complete = False
            break
        if status == "4" and not redo:
            cancelled += 1
        for code, title, auc in found:
            if auc is None:  # ヤフーショッピングの明細
                sku = yahoo_sku(code)
                if sku:
                    added += add_sold(state, sku, "yahoo_store", oid, now.isoformat())
                else:
                    odd.append(f"{oid}:{code}")
                continue
            auction += 1
            skus, why = auc
            if skus is None:
                unmatched.append(f"{oid} {title[:30]}（{why}）")
            elif not skus:
                auction_stopped += 1
            for sku in skus or []:
                auction_added += add_sold(state, sku, "yahoo_auction", oid, now.isoformat())
        if not redo:
            new.append(oid)
    st["processed"] = (st["processed"] + new)[-20000:]
    if complete:
        st["last_checked"] = now.isoformat()
        if backfill:
            st["auction_from"] = start.isoformat()  # ここから後のストアオークションの明細は見た
    log.info(f"ヤフーショッピングの注文: 取得 {len(orders)}件 / 新規 {len(new)}件 / 新たに売れたSKU {added}件"
             + (f" / キャンセル {cancelled}件" if cancelled else ""))
    if auction:
        log.info(f"ヤフオク店（ストアオークション）の明細 {auction}件{'（対応前の注文の見直しを含む）' if backfill else ''}: "
                 f"新たに売れたSKU {auction_added}件" + (f" / 同じ商品名がすべて在庫0 {auction_stopped}件" if auction_stopped else ""))
    if unmatched:
        log.warning(f"ヤフオク店: 商品番号を決められず在庫0にしていない明細 {len(unmatched)}件（手動で確認してください）: "
                    + " / ".join(unmatched[:20]))
    if odd:
        log.warning(f"ヤフーショッピング: 商品番号の形でない商品コード {len(odd)}件は在庫0にしていません: {', '.join(odd[:10])}")


def collect_yahoo_shopping_files(state, now, dry):
    """inbox/yahoo_shopping/ に置いたストアクリエイターProの注文データから、ヤフーショッピングで売れたSKUを取る
    （注文APIが使えるようになるまでの代わり）。明細（default_all_items.csv）の商品コード（Product Code）を使い、
    ストアオークション（AUCTION…）の明細は使わない。注文（default_all_orders.csv）も一緒に置けば、キャンセルされた注文と
    開始時刻より前の注文を除く。ほかの手動ファイルと同じく、SKUを1行に1つ書いたテキストなども読める。処理後は done/ へ移す。"""
    folder = YAHOO_SHOPPING_INBOX
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "done").mkdir(exist_ok=True)
    paths = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in (".csv", ".txt", ".tsv"))
    if not paths:
        return
    since = datetime.fromisoformat(state["since"])
    items_files, orders, order_paths, others = [], {}, [], []
    for p in paths:
        reader = csv.DictReader(io.StringIO(read_text_any(p)))
        fields = set(reader.fieldnames or [])
        if {"Order ID", "Line ID", "Product Code"} <= fields:
            items_files.append((p, list(reader)))
        elif {"Order ID", "Status", "Date"} <= fields:
            orders.update({r["Order ID"].strip(): r for r in reader if (r.get("Order ID") or "").strip()})
            order_paths.append(p)
        else:
            others.append(p)
    for p, rows in items_files:
        found, added, auction, cancelled, old, odd = 0, 0, 0, 0, 0, []
        for r in rows:
            oid, code = (r.get("Order ID") or "").strip(), (r.get("Product Code") or "").strip()
            if code.upper().startswith("AUCTION"):
                auction += 1
                continue
            o = orders.get(oid) or {}
            t = parse_order_time(o.get("Date"))
            if "キャンセル" in (o.get("Status") or ""):
                cancelled += 1
            elif t and t < since:
                old += 1
            elif yahoo_sku(code):
                found += 1
                added += add_sold(state, yahoo_sku(code), "yahoo_store", oid, now.isoformat())
            else:
                odd.append(f"{oid}:{code}")
        log.info(f"ヤフーショッピングの明細ファイル {p.name}: ヤフーショッピングの明細 {found}件 / 新たに売れたSKU {added}件"
                 + (f" / キャンセル {cancelled}件" if cancelled else "") + (f" / {since:%m/%d %H:%M} より前 {old}件" if old else "")
                 + (f" / ヤフオク店の明細 {auction}件は対象外" if auction else ""))
        if not orders:
            log.info("  注文データ（default_all_orders.csv）が無いため、キャンセルと注文日時では除いていません")
        if odd:
            log.warning(f"  商品番号の形でない商品コード {len(odd)}件は在庫0にしていません: {', '.join(odd[:10])}")
    for p in others:
        pairs = parse_manual_file(p, since)
        added = sum(add_sold(state, s, "yahoo_store", o or p.name, now.isoformat()) for s, o in pairs)
        log.info(f"ヤフーショッピングの手動ファイル {p.name}: SKU {len(pairs)}件 / 新たに売れたSKU {added}件")
    if order_paths and not items_files:
        # 注文データだけでは商品コードが分からない。明細が置かれるまで残しておく
        log.warning("ヤフーショッピング: 注文データだけでは商品コードが分かりません。明細（default_all_items.csv）も置いてください")
        paths = [p for p in paths if p not in order_paths]
    if not dry:
        for p in paths:
            shutil.move(str(p), str(folder / "done" / f"{now:%Y%m%d_%H%M%S}_{p.name}"))


# ---------- 送信 ----------

def pending(state, target):
    return [k for k, e in state["ledger"].items()
            if target in e["push"] and (e["push"][target] is None or str(e["push"][target]).startswith("error"))
            and e["tries"].get(target, 0) < MAX_RETRY]


def backfill_targets(state):
    """送り先を後から追加したとき、既存の台帳にもその送り先を「未送信」として加える。"""
    added = 0
    for e in state["ledger"].values():
        for t in targets_for(e["src"]):
            if t not in e["push"]:
                e["push"][t] = None
                added += 1
    return added


YAHOO_LOGIN_WARN_DAYS = 7  # ヤフーのログインの期限がこの日数以内になったら、毎回ログに警告を出す


def check_yahoo_login():
    """ヤフーのログイン（yahoo_auth.bat）の期限が近ければ警告する。リフレッシュトークンはログインから4週間で切れ、
    切れるとヤフーのストアへの在庫0（他で売れた分・ワサビで在庫0になった分）が止まる。"""
    try:
        import yahoo_shopping_api as yapi
        ycfg = yapi.load_config()
        deadline = yapi.reauth_deadline(ycfg)
    except SystemExit:  # 設定ファイルが無いなど（ヤフーのストアへの送信側で別に知らせる）
        return
    except Exception as e:
        log.warning(f"ヤフーのログインの期限を確認できませんでした: {e}")
        return
    if deadline is None:
        log.warning("ヤフーのログイン日時が記録されていないため、ログインの期限を確認できません"
                    "（yahoo_auth.bat でログインし直すと記録されます）")
        return
    left, when = deadline - time.time(), datetime.fromtimestamp(deadline, JST)
    if left <= 0:
        log.error(f"ヤフーのログインの期限（{when:%m/%d %H:%M} 頃）を過ぎています。yahoo_auth.bat でログインし直してください"
                  "（ヤフーのストアへの在庫0が止まります）")
    elif left <= YAHOO_LOGIN_WARN_DAYS * 86400:
        log.warning(f"ヤフーのログインの期限が近づいています: {when:%m/%d %H:%M} 頃（あと {left / 86400:.1f}日）。"
                    "yahoo_auth.bat でログインし直してください（切れるとヤフーのストアへの在庫0が止まります）")


def yahoo_store_split(ycfg, codes):
    """ヤフーのストアの在庫参照APIで、商品コードを (ストアに無い, すでに在庫0, 在庫が残っている) に分ける。"""
    import yahoo_shopping_api as yapi
    stock = yapi.get_stock(ycfg, codes)
    absent, already, to_zero = [], [], []
    for k in codes:
        s = stock.get(k) or {}
        if s.get("status") != "1":
            absent.append(k)
        elif s.get("quantity") == "0":
            already.append(k)
        else:
            to_zero.append(k)
    return absent, already, to_zero


def push_yahoo_store(state, dry, validate):
    """ヤフーのストア（Yahoo!ショッピング）で在庫0にする。在庫参照APIでストアにある・在庫が残っている商品だけを
    在庫更新APIで0にする。ストアに無い商品は absent として以後は送らない。"""
    todo = pending(state, "yahoo_store")
    if not todo:
        return
    try:
        import yahoo_shopping_api as yapi
        ycfg = yapi.load_config()
        absent, already, to_zero = yahoo_store_split(ycfg, todo)
    except SystemExit as e:
        log.warning(f"ヤフーのストア: 在庫を確認できないため今回は送りません（{e}）")
        return
    except Exception as e:
        log.error(f"ヤフーのストア: 在庫参照に失敗しました（次回再試行）: {e}")
        return
    log.info(f"ヤフーのストア: 在庫0にする {len(to_zero)}件 / すでに0 {len(already)}件 / ストアに無い {len(absent)}件")
    if dry or validate:
        if to_zero:
            log.info(f"  {'dry-run' if dry else '検証モード'}のためヤフーのストアへは送りません。例: {', '.join(to_zero[:10])}")
        return
    now = datetime.now(JST).isoformat()
    for k in absent:
        state["ledger"][k]["push"]["yahoo_store"] = "absent"
    for k in already:
        state["ledger"][k]["push"]["yahoo_store"] = now
    if not to_zero:
        return
    try:
        result = yapi.set_stock_zero(ycfg, to_zero)
    except Exception as e:
        log.error(f"ヤフーのストア: 在庫更新に失敗しました（次回再試行）: {e}")
        return
    failed = 0
    for k in to_zero:
        e = state["ledger"][k]
        e["tries"]["yahoo_store"] = e["tries"].get("yahoo_store", 0) + 1
        err = result.get(k, "応答なし")
        if err is None:
            e["push"]["yahoo_store"] = now
        else:
            e["push"]["yahoo_store"] = f"error:{err}"
            failed += 1
            log.error(f"ヤフーのストア 在庫0 失敗: {k} / {err}")
    log.info(f"ヤフーのストア 結果: 成功 {len(to_zero) - failed}件 / 失敗 {failed}件")


def push_wasabi(state, dry, validate):
    """ワサビの在庫を0にする。楽天の注文（rakuten_orders_sync）と同じく、FTP の /stock_import に「u,商品番号,0」の
    CSV を送る。ワサビが連携しているメルカリ・ラクマなどにもワサビから伝わる。"""
    todo = pending(state, "wasabi")
    if not todo:
        return
    log.info(f"ワサビへ在庫0: {len(todo)}件")
    if dry or validate:
        log.info(f"  {'dry-run' if dry else '検証モード'}のためワサビへは送りません。例: {', '.join(todo[:10])}")
        return
    c = configparser.ConfigParser()
    c.read(WASABI_FTP_CFG, encoding="utf-8")
    if not c.has_section("WASABI_FTP"):
        log.error(f"ワサビ: {WASABI_FTP_CFG.name} に [WASABI_FTP] が無いため送れません")
        return
    wf = c["WASABI_FTP"]
    now = datetime.now(JST)
    path = ws.OUT_DIR / f"wasabi_stock_yahoo_{now:%Y%m%d_%H%M%S}.csv"
    eol = "\r\n" if wf.get("newline", "LF").upper() == "CRLF" else "\n"
    lines = ["コントロールカラム,商品番号,在庫数"] + [f"u,{k},0" for k in sorted(todo)]
    path.write_bytes((eol.join(lines) + eol).encode("cp932", errors="replace"))
    result = now.isoformat()
    try:
        with ftplib.FTP() as ftp:  # 一時名で送ってから正式名にする（取り込みの途中読みを防ぐ）
            ftp.connect(wf["host"], int(wf.get("port", "21") or 21), timeout=60)
            ftp.login(wf["username"], wf["password"])
            ftp.cwd(wf["remote_dir"])
            with open(path, "rb") as f:
                ftp.storbinary(f"STOR {path.name}.tmp", f)
            ftp.rename(f"{path.name}.tmp", path.name)
        log.info(f"ワサビFTPへ送信: {wf['remote_dir']}/{path.name}（{len(todo)}件）")
    except Exception as e:
        result = f"error:FTP {type(e).__name__}"
        log.error(f"ワサビFTPへの送信に失敗しました（次回再試行）: {e}")
    for k in todo:
        e = state["ledger"][k]
        e["tries"]["wasabi"] = e["tries"].get("wasabi", 0) + 1
        e["push"]["wasabi"] = result


# ---------- ワサビで在庫0になった商品 → ヤフーのストアでも0 ----------
# ワサビはヤフーのストアへ在庫0を送っていない（他で売れた商品がヤフーで売れてしまう）。毎時の wasabi_stock_sync が
# 楽天に在庫0を送ったSKUを ws_zero_queue.txt に追記するので、それを受け取ってヤフーのストアでも0にする。
# 台帳（ledger）とは別に state["ws_zero"]（{SKU: 失敗回数}）で持ち、済んだものは消す。

def collect_ws_zero(state, dry):
    """ws_zero_queue.txt を受け取り、ヤフーのストアで0にする候補に加える。"""
    pend = state.setdefault("ws_zero", {})
    queue, work = ws.ZERO_QUEUE, ws.ZERO_QUEUE.with_suffix(".work")
    if dry:  # dry-run では受け取らずに件数だけ見る
        skus = {norm(s) for p in (queue, work) if p.exists() for s in p.read_text(encoding="utf-8").split()}
        if skus:
            log.info(f"ワサビで在庫0になったSKU: 受け取り待ち {len(skus)}件（dry-run のため受け取りません）")
        return
    if not work.exists():  # .work が残っていれば前回の受け取り途中。先にそれを読む
        if not queue.exists():
            return
        try:
            queue.replace(work)
        except OSError:  # wasabi_stock_sync が書き込み中。次回受け取る
            return
    skus = {norm(s) for s in work.read_text(encoding="utf-8").split() if s.strip()}
    new = [s for s in skus if s not in pend]
    pend.update({s: 0 for s in new})
    log.info(f"ワサビで在庫0になったSKU: 受け取り {len(skus)}件 / ヤフーのストアで0にする候補 {len(pend)}件")
    save_state(state)  # 保存してから受け取りファイルを消す（途中で止まっても失わない）
    work.unlink()


def ws_stock_check(wcfg, codes):
    """ワサビの在庫を商品番号でまとめて確かめ、(在庫0, 在庫あり, ワサビに無い) に分ける。"""
    found = {}
    for i in range(0, len(codes), ws.RECHECK_BATCH):
        batch = codes[i:i + ws.RECHECK_BATCH]
        page, got = 1, 0
        while True:
            data = ws.fetch_page(wcfg, page, {"商品番号": {"in": batch}})
            items = data.get("data", [])
            for it in items:
                sku = norm(str(it.get("商品番号") or ""))
                q = ws.parse_stock(it.get("在庫数"))
                found[sku] = max(found.get(sku, 0), 1 if q is None else q)  # 在庫数が読めなければ在庫ありとみなす
            got += len(items)
            if not items or got >= (data.get("total") or 0):
                break
            page += 1
            time.sleep(0.4)
        time.sleep(0.4)
    return ([c for c in codes if found.get(c) == 0], [c for c in codes if found.get(c, 0) > 0],
            [c for c in codes if c not in found])


def push_ws_zero_to_yahoo(state, cfg, dry, validate):
    """ワサビで在庫0になったSKUを、ヤフーのストアでも0にする。送る直前にワサビの在庫を確かめ直し、
    在庫が残っているもの（楽天の出品をやめただけなど）やワサビに無いものは送らない。"""
    pend = state.get("ws_zero") or {}
    if not pend:
        return
    codes = sorted(pend)
    try:
        import yahoo_shopping_api as yapi
        zero, in_stock, unknown = ws_stock_check(cfg, codes)
        ycfg = yapi.load_config()
        absent, already, to_zero = yahoo_store_split(ycfg, zero) if zero else ([], [], [])
    except SystemExit as e:
        log.warning(f"ワサビで在庫0になったSKU: 確認できないため今回はヤフーのストアへ送りません（{e}）")
        return
    except Exception as e:
        log.error(f"ワサビで在庫0になったSKU: 在庫の確認に失敗しました（次回再試行）: {e}")
        return
    log.info(f"ワサビで在庫0になったSKU {len(codes)}件 → ヤフーのストアで0にする {len(to_zero)}件 / すでに0 {len(already)}件"
             f" / ストアに無い {len(absent)}件" + (f" / ワサビに在庫あり（送らない） {len(in_stock)}件" if in_stock else "")
             + (f" / ワサビに無い（送らない） {len(unknown)}件" if unknown else ""))
    if dry or validate:
        if to_zero:
            log.info(f"  {'dry-run' if dry else '検証モード'}のためヤフーのストアへは送りません。例: {', '.join(to_zero[:10])}")
        return
    for k in absent + already + in_stock + unknown:
        pend.pop(k, None)
    if not to_zero:
        return
    try:
        result = yapi.set_stock_zero(ycfg, to_zero)
    except Exception as e:
        log.error(f"ワサビで在庫0になったSKU: ヤフーのストアの在庫更新に失敗しました（次回再試行）: {e}")
        return
    failed = []
    for k in to_zero:
        err = result.get(k, "応答なし")
        if err is None:
            pend.pop(k, None)
            continue
        pend[k] += 1
        failed.append(f"{k}（{err}）")
        if pend[k] >= MAX_RETRY:
            log.error(f"ヤフーのストア 在庫0 再試行の上限: {k} / {err}（手動で確認してください）")
            pend.pop(k, None)
    log.info(f"ヤフーのストア 結果（ワサビで在庫0の分）: 成功 {len(to_zero) - len(failed)}件 / 失敗 {len(failed)}件"
             + (f": {', '.join(failed[:10])}" if failed else ""))


def push_rakuten(state, cfg, dry, validate):
    """楽天の在庫CSV（WS連携と同じ形式）で在庫0を送る。楽天に出品が無いSKUは送らない。"""
    todo = pending(state, "rakuten")
    if not todo:
        return
    catalog = set(json.loads(ws.STATE_FILE.read_text(encoding="utf-8"))) if ws.STATE_FILE.exists() else None
    send = {}
    for k in todo:
        if catalog is not None and k.lower() not in catalog:
            state["ledger"][k]["push"]["rakuten"] = "absent"
        else:
            send[k.lower()] = 0
    log.info(f"楽天へ在庫0: {len(send)}件（楽天に出品が無く送らない: {len(todo) - len(send)}件）")
    if not send or dry or validate:
        if send:
            log.info(f"  {'dry-run' if dry else '検証モード'}のため楽天へは送りません。例: {', '.join(list(send)[:10])}")
        return
    for path, part in ws.write_inventory_csvs(send):
        ok = ws.upload_sftp(cfg, path)
        for sku in part:
            e = state["ledger"][sku.upper()]
            e["tries"]["rakuten"] = e["tries"].get("rakuten", 0) + 1
            e["push"]["rakuten"] = datetime.now(JST).isoformat() if ok else "error:SFTP"
        save_state(state)


def push_amazon(state, acfg, dry, validate):
    todo = pending(state, "amazon")
    if not todo:
        return
    log.info(f"Amazonへ在庫0: {len(todo)}件{'（検証モード）' if validate else ''}")
    if dry:
        log.info(f"  dry-run のためAmazonへは送りません。例: {', '.join(todo[:10])}")
        return
    counts = {}
    for n, k in enumerate(todo, 1):
        res = amazon_zero(acfg, k, validate)
        counts[res.split(":")[0]] = counts.get(res.split(":")[0], 0) + 1
        if res.startswith("error"):
            log.error(f"Amazon 在庫0 失敗: {k} / {res}")
        if not validate:
            e = state["ledger"][k]
            e["tries"]["amazon"] = e["tries"].get("amazon", 0) + 1
            e["push"]["amazon"] = datetime.now(JST).isoformat() if res == "ok" else res
            if n % 20 == 0:
                save_state(state)
        time.sleep(0.25)  # patchListingsItem は 5リクエスト/秒
    log.info(f"Amazon 結果: 成功 {counts.get('ok', 0)}件 / 出品なし {counts.get('absent', 0)}件 / "
             f"FBAのみで対象外 {counts.get('fba', 0)}件 / 失敗 {counts.get('error', 0)}件")


def rakuten_catalog():
    """楽天に出品がある商品番号（小文字）の集合。WS連携の送信状態ファイルのキーを使う（読むだけ）。"""
    return set(json.loads(ws.STATE_FILE.read_text(encoding="utf-8"))) if ws.STATE_FILE.exists() else None


AMAZON_VERIFY_AFTER = timedelta(minutes=30)  # Amazon は受付後の反映が遅いことがあるため、この時間がたってから確認する
AMAZON_RESEND_MAX = 3


def verify_amazon(state, acfg, now, dry):
    """在庫0を送ってから一定時間たったSKUについて、Amazon の在庫が本当に0になったかを確かめ、
    まだ在庫があれば送り直す。確認できたものは "verified" を記録して以後は見ない。"""
    due = [k for k, e in state["ledger"].items()
           if str(e["push"].get("amazon", "")).startswith("20") and not e.get("verified", {}).get("amazon")
           and now - datetime.fromisoformat(e["push"]["amazon"]) >= AMAZON_VERIFY_AFTER]
    if not due:
        return
    ok, resent, gave_up = 0, [], []
    for k in due:
        e = state["ledger"][k]
        try:
            li = sp_request(acfg, "GET", f"/listings/2021-08-01/items/{acfg['seller_id']}/{quote(k, safe='')}",
                            params={"marketplaceIds": acfg["marketplace_id"], "includedData": "fulfillmentAvailability"})
        except RuntimeError as ex:
            if "HTTP 404" in str(ex):
                e.setdefault("verified", {})["amazon"] = "absent"
            continue
        qty = [f.get("quantity") or 0 for f in li.get("fulfillmentAvailability") or [] if f.get("fulfillmentChannelCode") == "DEFAULT"]
        if not any(q > 0 for q in qty):
            if not dry:
                e.setdefault("verified", {})["amazon"] = now.isoformat()
            ok += 1
        elif e.get("resent", {}).get("amazon", 0) >= AMAZON_RESEND_MAX:
            gave_up.append(k)
        else:
            if not dry:
                res = amazon_zero(acfg, k, False)
                e.setdefault("resent", {})["amazon"] = e.get("resent", {}).get("amazon", 0) + 1
                e["push"]["amazon"] = now.isoformat() if res == "ok" else res
            resent.append(k)
        time.sleep(0.25)
    log.info(f"Amazon の反映確認: 0を確認 {ok}件 / まだ在庫あり{'（dry-run のため再送しない）' if dry else '・再送'} {len(resent)}件"
             f"（例: {', '.join(resent[:5])}）")
    if gave_up:
        log.error(f"Amazon で{AMAZON_RESEND_MAX}回送っても在庫が0にならないSKU: {len(gave_up)}件（セラーセントラルで手動確認してください）: "
                  f"{', '.join(gave_up[:20])}")


def write_todo(state, now, dry, upload_malls=()):
    """メルカリ・ヤフオクで手動で止めるSKUの一覧（今回の新規分と累計）を Excel で開ける CSV で作る。
    upload_malls（在庫ありリストがありアップロード用ファイルで対応するモール）は一覧を作らない。
    メルカリのリストが無いときは、楽天と同じ商品を出品している想定で、楽天に出品が無いSKUは一覧に入れない。"""
    TODO_DIR.mkdir(exist_ok=True)
    catalog = rakuten_catalog()
    for mall, label in MANUAL_MALLS.items():
        if mall in TODO_STOPPED:
            continue
        if mall in upload_malls:
            new = [e for e in state["ledger"].values() if mall in e["todo"] and e["todo"][mall] is None]
            if not dry:
                for e in new:
                    e["todo"][mall] = "アップロード用ファイルで対応"
            if new:
                log.info(f"{label}: 在庫ありリストがあるため、止める一覧は作らずアップロード用ファイルで対応します（新規 {len(new)}件）")
            continue
        if mall in MALLS_LIKE_RAKUTEN and catalog is not None:
            skipped = 0
            for k, e in state["ledger"].items():
                if mall in e["todo"] and k.lower() not in catalog:
                    if e["todo"][mall] is None:
                        skipped += 1
                    if not dry:
                        e["todo"][mall] = "楽天に無いため対象外"
            if skipped:
                log.info(f"{label}: 楽天に出品が無いSKU {skipped}件は一覧に入れません（{label}は楽天と同じ出品の想定）")
        rows = sorted(((k, e) for k, e in state["ledger"].items() if mall in e["todo"]
                       and not (mall in MALLS_LIKE_RAKUTEN and catalog is not None and k.lower() not in catalog)),
                      key=lambda x: x[1]["at"])
        new = [(k, e) for k, e in rows if e["todo"][mall] is None]
        if dry:
            log.info(f"{label}で止める一覧: 今回の新規 {len(new)}件（確認モードのためファイルは作りません）")
            continue
        header = ["商品番号", "売れたモール", "注文番号", "検知日時", "一覧に出した日時"]

        def line(k, e):
            return [k, MALL_LABEL[e["src"]], e["order"], e["at"][:16].replace("T", " "), (e["todo"][mall] or "")[:16].replace("T", " ")]

        if new:
            path = TODO_DIR / f"{label}_止める_{now:%Y%m%d_%H%M}.csv"
            with open(path, "w", newline="", encoding="cp932", errors="replace") as f:
                w = csv.writer(f)
                w.writerow(header)
                for k, e in new:
                    w.writerow(line(k, e))
            log.info(f"{label}で止める一覧（今回の新規 {len(new)}件）: {path.name}")
            if not dry:
                for k, e in new:
                    e["todo"][mall] = now.isoformat()
        with open(TODO_DIR / f"{label}_止める_累計.csv", "w", newline="", encoding="cp932", errors="replace") as f:
            w = csv.writer(f)
            w.writerow(header)
            for k, e in rows:
                w.writerow(line(k, e))


MERCARI_PRODUCTS_DIR = INBOX / "mercari_products"
REFERENCE_DIR = TODO_DIR / "参考"
MERCARI_SKU_COL = re.compile(r"^SKU(\d+)_(ID|商品管理コード|現在の在庫数)$")


def read_mercari_product_file(path, products):
    """メルカリShopsの商品CSV 1ファイルを読み、products に {商品管理コード(大文字): (商品ID, [(SKU番号, SKU_ID, 現在の在庫数, 商品管理コード), ...])} を追加する。
    戻り値は (文字コード, 読んだ商品数)。形式が違うときは (None, 0)。"""
    raw = path.read_bytes()
    enc = "utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8"
    try:
        text = raw.decode(enc)
    except UnicodeDecodeError:
        enc, text = "cp932", raw.decode("cp932", errors="replace")
    rows = csv.reader(io.StringIO(text))
    header = [h.strip() for h in next(rows, [])]
    if "商品ID" not in header:
        log.error(f"{path.name} に「商品ID」列がありません。見出し: {header[:15]}")
        return None, 0
    i_pid = header.index("商品ID")
    sku_cols = {}
    for i, h in enumerate(header):
        m = MERCARI_SKU_COL.match(h)
        if m:
            sku_cols.setdefault(int(m.group(1)), {})[m.group(2)] = i
    if not any("商品管理コード" in c and "ID" in c for c in sku_cols.values()):
        log.error(f"{path.name} に「SKU1_ID」「SKU1_商品管理コード」列が見つかりません。見出し: {header[:40]}")
        return None, 0
    count = 0
    for row in rows:
        if len(row) <= i_pid or not row[i_pid].strip():
            continue
        skus = []
        for n in sorted(sku_cols):
            c = sku_cols[n]
            sku_id = row[c["ID"]].strip() if "ID" in c and len(row) > c["ID"] else ""
            if not sku_id:
                continue
            code = row[c["商品管理コード"]].strip() if "商品管理コード" in c and len(row) > c["商品管理コード"] else ""
            stock = ws.parse_stock(row[c["現在の在庫数"]]) if "現在の在庫数" in c and len(row) > c["現在の在庫数"] else None
            skus.append((n, sku_id, stock, code))
        for n, sku_id, stock, code in skus:
            if code:
                products[norm(code)] = (row[i_pid].strip(), skus)
        count += 1
    return enc, count


def load_mercari_products():
    """inbox/mercari_products/ の商品CSV（メルカリShopsの一括編集用ダウンロード。複数ファイルに分けてよい）をすべて読み、
    {商品管理コード(大文字): (商品ID, [...])} と、ファイル群の識別名・文字コードを返す。"""
    MERCARI_PRODUCTS_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted((p for p in MERCARI_PRODUCTS_DIR.iterdir() if p.is_file() and p.suffix.lower() in (".csv", ".txt")),
                   key=lambda p: p.stat().st_mtime)
    if not files:
        return None, None, None
    products, encs, rows = {}, set(), 0
    for path in files:
        enc, count = read_mercari_product_file(path, products)
        if enc:
            encs.add(enc)
            rows += count
    if not encs:
        return None, None, None
    newest = datetime.fromtimestamp(files[-1].stat().st_mtime)
    age = datetime.now() - newest
    log.info(f"メルカリの商品CSV {len(files)}ファイルを読みました（商品 {rows:,}行 / 商品管理コード {len(products):,}件 / "
             f"最新ファイルは{age.total_seconds() / 3600:.1f}時間前）")
    if age > timedelta(hours=6):
        log.warning("メルカリの商品CSVが6時間以上前のものです。現在の在庫数が古いとアップロードでエラーになることがあります")
    enc = "cp932" if "cp932" in encs else sorted(encs)[0]
    return products, f"{len(files)}files_{newest:%Y%m%d%H%M%S}", enc


def write_mercari_upload(state, now, dry, loaded=None):
    """他モールで売れたSKUを、メルカリShopsの在庫一括更新CSV（増減フラグ=2:マイナス、在庫増減数=現在の在庫数）にする。
    一度ファイルに出したSKUは二度と出さない（二重に減らさないため）。"""
    products, export_name, enc = loaded or load_mercari_products()
    if products is None:
        log.info("メルカリのアップロード用ファイル: inbox/mercari_products/ に商品CSVが無いため作りません")
        return
    targets = [(k, e) for k, e in state["ledger"].items() if "mercari" in e["todo"]
               and (not e.get("mercari_upload") or (e["mercari_upload"].get("status") in ("absent", "zero")
                                                    and e["mercari_upload"].get("export") != export_name))]
    rows, absent, zero = [], 0, 0
    for k, e in sorted(targets, key=lambda x: x[1]["at"]):
        hit = products.get(k)
        if not hit:
            absent += 1
            if not dry:
                e["mercari_upload"] = {"status": "absent", "export": export_name}
            continue
        pid, skus = hit
        target = next(s for s in skus if norm(s[3]) == k)
        if target[0] != 1:
            # 在庫更新ファイルは SKU1 の5項目だけで作るため、SKU2 以降の商品は手作業
            log.error(f"メルカリ: {k} は商品 {pid} の SKU{target[0]} のため、在庫更新ファイルに入れられません（手作業で0にしてください）")
            if not dry:
                e["mercari_upload"] = {"status": "multi_sku", "export": export_name}
            continue
        if not target[2]:
            zero += 1
            if not dry:
                e["mercari_upload"] = {"status": "zero", "export": export_name}
            continue
        rows.append((k, [pid, target[1], str(target[2]), "2", str(target[2])]))  # 2: マイナス、現在の在庫数だけ減らして 0 にする
    log.info(f"メルカリの在庫更新: 対象 {len(rows)}件 / メルカリの商品CSVに無い {absent}件 / すでに在庫0 {zero}件")
    if not dry:
        # 商品CSVに無かったSKU（メルカリに出品が無い・CSVの出力範囲外）の参考一覧
        missing = sorted(((k, e) for k, e in state["ledger"].items()
                          if (e.get("mercari_upload") or {}).get("status") == "absent"), key=lambda x: x[1]["at"])
        REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
        with open(REFERENCE_DIR / "メルカリ_商品CSVに無い.csv", "w", newline="", encoding="cp932", errors="replace") as f:
            w = csv.writer(f, lineterminator="\r\n")
            w.writerow(["商品番号", "売れたモール", "注文番号", "検知日時"])
            for k, e in missing:
                w.writerow([k, MALL_LABEL[e["src"]], e["order"], e["at"][:16].replace("T", " ")])
    if not rows or dry:
        return
    path, total = write_mercari_upload_file(now, enc, [line for _, line in rows])
    for k, _ in rows:
        state["ledger"][k]["mercari_upload"] = {"status": "file", "file": path.name, "at": now.isoformat(), "export": export_name}
    log.info(f"メルカリのアップロード用ファイルを作成: {path.name}（今回の新規 {len(rows)}件 / 残っていたファイルと合わせて {total}件）")


MERCARI_UPLOAD_HEADER = ["商品ID", "SKU1_ID", "SKU1_現在の在庫数", "SKU1_増減フラグ", "SKU1_在庫増減数"]


def write_mercari_upload_file(now, enc, new_lines):
    """todo/ 直下に未アップロードの在庫更新ファイルが残っていれば、その行と新しい行を1つのファイルにまとめる。
    （アップロード済みのファイルは todo/Uploaded/ へ移す運用。サブフォルダの中は見ない）
    使用中などで動かせない古いファイルは結合しない（同じ行が2つのファイルに入らないようにするため）。"""
    leftovers = []
    for old in sorted(TODO_DIR.glob("メルカリ_在庫更新_アップロード_*.csv")):
        tmp = old.with_suffix(".merging")
        try:
            old.rename(tmp)
        except OSError:
            log.warning(f"{old.name} を開いているなどで動かせないため、今回は結合しません")
            continue
        leftovers.append(tmp)
    lines, seen = [], set()
    for tmp in leftovers:
        with open(tmp, "r", encoding=enc, errors="replace", newline="") as f:
            reader = csv.reader(f)
            next(reader, None)
            for row in reader:
                if len(row) >= 5 and row[1] and row[1] not in seen:
                    seen.add(row[1])
                    lines.append(row[:5])
    for line in new_lines:
        if line[1] not in seen:
            seen.add(line[1])
            lines.append(line)
    path = TODO_DIR / f"メルカリ_在庫更新_アップロード_{now:%Y%m%d_%H%M}.csv"
    with open(path, "w", newline="", encoding=enc, errors="replace") as f:
        w = csv.writer(f, lineterminator="\r\n")
        w.writerow(MERCARI_UPLOAD_HEADER)
        w.writerows(lines)
    for tmp in leftovers:
        tmp.unlink(missing_ok=True)
    if leftovers:
        log.info(f"残っていた在庫更新ファイル {len(leftovers)}つを結合しました")
    return path, len(lines)


def ws_export(state):
    """WS 復旧時に、停止中に売れたSKUを WS へ取り込ませるCSV（rakuten_orders_sync.py と同じ形式）を作る。"""
    path = ws.OUT_DIR / f"wasabi_stock_停止中に売れた分_{datetime.now():%Y%m%d_%H%M%S}.csv"
    lines = ["コントロールカラム,商品番号,在庫数"] + [f"u,{k},0" for k in sorted(state["ledger"])]
    path.write_bytes(("\r\n".join(lines) + "\r\n").encode("cp932", errors="replace"))
    log.info(f"WS 取り込み用CSVを作成: {path}（{len(state['ledger'])}件）。WS 復旧後に /stock_import へ送ってください")


def main():
    args = sys.argv[1:]
    dry, validate = "--dry-run" in args, "--validate" in args
    since = None
    if "--since" in args:
        since = datetime.fromisoformat(args[args.index("--since") + 1])
        if since.tzinfo is None:
            since = since.replace(tzinfo=JST)
    state = load_state(since)
    if "--ws-export" in args:
        ws_export(state)
        return
    if "--mercari-upload" in args:
        # 注文の取得はせず、台帳からメルカリのアップロード用ファイルだけ作る
        write_mercari_upload(state, datetime.now(JST), dry)
        if not dry:
            save_state(state)
        return
    now = datetime.now(JST)
    log.info(f"モード: {'dry-run' if dry else '検証' if validate else '送信'} / 対象: {state['since']} 以降の注文 / 台帳 {len(state['ledger'])}件")
    check_yahoo_login()

    acfg = load_amazon_config()
    cfg = ws.load_configs()
    added = backfill_targets(state)
    if added:
        log.info(f"既存の台帳に新しい送り先を追加: {added}件")
    collect_rakuten(state, now, dry)
    collect_amazon(state, acfg, now)
    collect_manual(state, now, dry)
    collect_yahoo_shopping(state, now, cfg)
    collect_yahoo_shopping_files(state, now, dry)
    collect_ws_zero(state, dry or validate)
    if not dry and not validate:
        save_state(state)  # 取得した注文を先に保存（送信は未送信フラグで次回も再試行される）

    push_amazon(state, acfg, dry, validate)
    if not validate:
        verify_amazon(state, acfg, now, dry)
    push_rakuten(state, cfg, dry, validate)
    push_yahoo_store(state, dry, validate)
    push_ws_zero_to_yahoo(state, cfg, dry, validate)
    push_wasabi(state, dry, validate)
    if MERCARI_UPLOAD:
        mercari_list = load_mercari_products()  # メルカリの在庫ありリスト（あればメルカリはアップロード用ファイルで対応）
        write_todo(state, now, dry or validate, upload_malls={"mercari"} if mercari_list[0] is not None else set())
        write_mercari_upload(state, now, dry or validate, mercari_list)
    else:
        write_todo(state, now, dry or validate)
    if not dry and not validate:
        save_state(state)

    stuck = {t: len([k for k, e in state["ledger"].items() if str(e["push"].get(t, "")).startswith("error")
                     and e["tries"].get(t, 0) >= MAX_RETRY]) for t in AUTO_TARGETS}
    if any(stuck.values()):
        log.error(f"再試行の上限に達した失敗: {stuck}（ログを確認して手動で対応してください）")
    log.info(f"完了。台帳（停止中に売れたSKU）: {len(state['ledger'])}件")


def acquire_lock():
    if LOCK_FILE.exists() and time.time() - LOCK_FILE.stat().st_mtime < 2 * 3600:
        log.warning("別の実行が進行中のため、今回は中止します")
        return False
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
