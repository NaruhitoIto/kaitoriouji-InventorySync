# -*- coding: utf-8 -*-
"""
ワサビスイッチ(WS)停止中の代替: WS を通さず、モール間で直接「売れた商品の在庫を0」にする。

  楽天の注文（RMS API）              → Amazon の在庫を0（Listings API）
  Amazonの注文（SP-API）             → 楽天の在庫を0（RMS 在庫CSV を SFTP）
  メルカリ・ヤフオクの注文（手動ファイル）→ 楽天・Amazon の在庫を0
  どこかで売れたSKU                  → メルカリ・ヤフオクで手動で止める一覧（todo/）を作る

在庫は「0にする」方向しか送らない（売れた1点物を止めるだけ）。キャンセル・返品で戻すのは手動。
WS の状態ファイル（wasabi_stock_state.json）は読むだけで変更しない（WS 復旧後の処理を壊さないため）。

手動ファイルの置き場所（処理後は done/ に移動）:
  inbox/mercari/  メルカリShopsの注文CSV、またはSKUを1行に1つ書いたテキスト
  inbox/yahoo/    ヤフオクの注文（落札）CSV、またはSKUを1行に1つ書いたテキスト
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
  リストに無いSKUはメルカリに出品が無いものとして扱う（参考に todo/メルカリ_商品CSVに無い.csv を出す）。
"""
import base64
import configparser
import csv
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

import requests

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
AUTO_TARGETS = ("rakuten", "amazon")  # API で自動的に0にするモール
MANUAL_MALLS = {"mercari": "メルカリ", "yahoo": "ヤフオク"}  # 手動で止めるモール（一覧を作る）
MALL_LABEL = {"rakuten": "楽天", "amazon": "Amazon", **MANUAL_MALLS}
MALLS_LIKE_RAKUTEN = {"mercari"}  # 楽天と同じ商品を出品している想定のモール（止める一覧を楽天の出品で絞る）
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
    for t in AUTO_TARGETS:
        if t != src:
            entry["push"][t] = None  # None = 未送信
    for m in MANUAL_MALLS:
        if m != src:
            entry["todo"][m] = None  # None = まだ一覧に出していない
    state["ledger"][key] = entry
    return True


# ---------- 楽天（注文取得） ----------

def rakuten_headers():
    c = configparser.ConfigParser()
    c.read(RAKUTEN_CFG, encoding="utf-8")
    rk = c["RAKUTEN_API"]
    token = base64.b64encode(f"{rk['service_secret'].strip()}:{rk['license_key'].strip()}".encode()).decode()
    return {"Authorization": f"ESA {token}", "Content-Type": "application/json; charset=UTF-8"}


def rakuten_orders(start, end):
    """注文日時が期間内の注文を取得し、[(注文番号, 進捗, [SKU...])] を返す。"""
    h = rakuten_headers()
    result, page = [], 1
    while True:
        body = {"version": 6, "dateType": 1,
                "startDatetime": start.strftime("%Y-%m-%dT%H:%M:%S%z"), "endDatetime": end.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "PaginationRequestModel": {"requestRecordsAmount": 100, "requestPage": page}}
        r = requests.post("https://api.rms.rakuten.co.jp/es/2.0/order/searchOrder/", headers=h, json=body, timeout=60)
        if r.status_code != 200:
            raise RuntimeError(f"RMS searchOrder HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        if any(m.get("messageType") == "ERROR" for m in data.get("MessageModelList") or []):
            raise RuntimeError(f"RMS searchOrder エラー: {data['MessageModelList']}")
        numbers = data.get("orderNumberList") or []
        if numbers:
            d = requests.post("https://api.rms.rakuten.co.jp/es/2.0/order/getOrder/", headers=h,
                              json={"orderNumberList": numbers, "version": 6}, timeout=60)
            if d.status_code != 200:
                raise RuntimeError(f"RMS getOrder HTTP {d.status_code}: {d.text[:300]}")
            for o in d.json().get("OrderModelList", []):
                skus = [it.get("itemNumber") or it.get("manageNumber")
                        for pkg in o.get("PackageModelList", []) for it in pkg.get("ItemModelList", [])]
                result.append((o.get("orderNumber"), str(o.get("orderProgress", "")), [s for s in skus if s]))
        total_pages = int((data.get("PaginationResponseModel") or {}).get("totalPages", 1) or 1)
        if page >= total_pages or not numbers:
            return result
        page += 1


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


# ---------- 送信 ----------

def pending(state, target):
    return [k for k, e in state["ledger"].items()
            if target in e["push"] and (e["push"][target] is None or str(e["push"][target]).startswith("error"))
            and e["tries"].get(target, 0) < MAX_RETRY]


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
        if not target[2]:
            zero += 1
            if not dry:
                e["mercari_upload"] = {"status": "zero", "export": export_name}
            continue
        rows.append((k, pid, skus, target))
    log.info(f"メルカリの在庫更新: 対象 {len(rows)}件 / メルカリの商品CSVに無い {absent}件 / すでに在庫0 {zero}件")
    if not dry:
        # 商品CSVに無かったSKU（メルカリに出品が無い・CSVの出力範囲外）の一覧。手作業で確認する用
        missing = sorted(((k, e) for k, e in state["ledger"].items()
                          if (e.get("mercari_upload") or {}).get("status") == "absent"), key=lambda x: x[1]["at"])
        TODO_DIR.mkdir(exist_ok=True)
        with open(TODO_DIR / "メルカリ_商品CSVに無い.csv", "w", newline="", encoding="cp932", errors="replace") as f:
            w = csv.writer(f, lineterminator="\r\n")
            w.writerow(["商品番号", "売れたモール", "注文番号", "検知日時"])
            for k, e in missing:
                w.writerow([k, MALL_LABEL[e["src"]], e["order"], e["at"][:16].replace("T", " ")])
    if not rows or dry:
        return
    max_n = max(s[0] for _, _, skus, _ in rows for s in skus)
    header = ["商品ID"]
    for n in range(1, max_n + 1):
        header += [f"SKU{n}_ID", f"SKU{n}_現在の在庫数", f"SKU{n}_増減フラグ", f"SKU{n}_在庫増減数"]
    path = TODO_DIR / f"メルカリ_在庫更新_アップロード_{now:%Y%m%d_%H%M}.csv"
    TODO_DIR.mkdir(exist_ok=True)
    with open(path, "w", newline="", encoding=enc, errors="replace") as f:
        w = csv.writer(f, lineterminator="\r\n")
        w.writerow(header)
        for k, pid, skus, target in rows:
            line = [pid] + [""] * (4 * max_n)
            for n, sku_id, stock, code in skus:
                base = 1 + 4 * (n - 1)
                line[base] = sku_id
                line[base + 1] = "" if stock is None else str(stock)
                if n == target[0]:
                    line[base + 2] = "2"           # 2: マイナス
                    line[base + 3] = str(stock)    # 現在の在庫数だけ減らして 0 にする
            w.writerow(line)
    for k, pid, skus, target in rows:
        state["ledger"][k]["mercari_upload"] = {"status": "file", "file": path.name, "at": now.isoformat(), "export": export_name}
    log.info(f"メルカリのアップロード用ファイルを作成: {path.name}（{len(rows)}件）")


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

    acfg = load_amazon_config()
    cfg = ws.load_configs()
    collect_rakuten(state, now, dry)
    collect_amazon(state, acfg, now)
    collect_manual(state, now, dry)
    if not dry and not validate:
        save_state(state)  # 取得した注文を先に保存（送信は未送信フラグで次回も再試行される）

    push_amazon(state, acfg, dry, validate)
    if not validate:
        verify_amazon(state, acfg, now, dry)
    push_rakuten(state, cfg, dry, validate)
    mercari_list = load_mercari_products()  # メルカリの在庫ありリスト（あればメルカリはアップロード用ファイルで対応）
    write_todo(state, now, dry or validate, upload_malls={"mercari"} if mercari_list[0] is not None else set())
    write_mercari_upload(state, now, dry or validate, mercari_list)
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
