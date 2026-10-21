# -*- coding: utf-8 -*-
"""
出荷データ作成アプリ（ワサビスイッチ停止中の代替）

各モールの「出荷できる注文」を、ワサビスイッチが出力していた注文CSV（order-YYYYMMDDHHMMSS.csv と同じ65列・UTF-8）に
まとめて出力する。出力したファイルを出荷システムに取り込んでピッキング・梱包を行う。

  楽天     : RMS 受注API で「発送待ち（進捗300）」の注文を取得
  Amazon   : セラーセントラルの注文レポート（未出荷の注文）ファイル
  メルカリ : メルカリShopsの注文CSV（orders_*.csv）
  ヤフオク : ヤフオクの注文CSV（YahooAuctionId, OrderId, ... の形式）。入金済み（PayStatus=1）だけ出力

・注文ファイルは「注文ファイルを追加」で選ぶ（どのモールのファイルかは列名から自動で判定）
・「この日時以降の注文」より前の注文は出力しない
・一度出力した注文は「出力履歴.json」に記録し、次回からは出力しない（二重出荷を防ぐ）
・設定（楽天APIのキー、モール名など）は exe と同じフォルダの shipping_export.ini
"""
import base64
import configparser
import csv
import io
import json
import os
import re
import sys
import threading
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

APP_NAME = "出荷データ作成"
APP_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
CONFIG_FILE = APP_DIR / "shipping_export.ini"
HISTORY_FILE = APP_DIR / "出力履歴.json"
JST = timezone(timedelta(hours=9))

# ワサビスイッチの注文CSVと同じ列（65列）
HEADER = [
    "会員ID", "ステータス", "モール", "注文番号", "受注日", "お名前", "お名前(フリガナ)", "メールアドレス", "国", "都道府県",
    "郵便番号", "市区郡", "町名・番地・建物名", "TEL", "お支払方法", "入金日", "発送日", "お届け日指定", "お届け時間帯指定",
    "領収書宛名", "領収書但し書き", "ラッピング1", "ラッピング2", "備考", "メモ", "通信事項", "宅配前の事前連絡※", "YahooID",
    "eBay Feedback送信日時", "総重量", "お届け先：お名前", "お届け先：お名前(フリガナ)", "お届け先：TEL", "お届け先：国",
    "お届け先：都道府県", "お届け先：郵便番号", "お届け先：市区郡", "お届け先：町名・番地・建物名", "お届け先：配送業者",
    "お届け先：お荷物伝票番号", "小計", "値引き", "消費税", "送料", "ラッピング１料金", "ラッピング２料金", "代引き手数料",
    "使用ポイント", "合計", "請求合計", "商品名", "商品コード", "商品金額", "商品数", "注文ID", "消込日", "親品番",
    "キャンセル日", "規格1種別", "規格2種別", "カード支払い回数", "クレジットカード支払方法", "規格1", "規格2",
    "ship-service-level",
]

DEFAULT_CONFIG = """\
; 出荷データ作成アプリの設定。exe と同じフォルダに置く。楽天のキーはチャットやGitHubに貼らないこと。
[RAKUTEN_API]
; RMS の service_secret と license_key（空なら楽天の取得はしない）
service_secret =
license_key =
; 既存の設定ファイル（[RAKUTEN_API] がある ini）を使う場合はそのパス。キーが空のときだけ使う
config_file =

[SETTINGS]
; 出力先フォルダ（相対パスなら exe と同じフォルダの中）
output_dir = 出荷データ
; ステータス列とお届け先：配送業者列に入れる値
status = 本日発送分
carrier = 日本郵便
; モール列に入れる名前（出荷システムが使っている名前）
rakuten_mall = 楽天市場店
amazon_mall = Amazon店
mercari_mall = メルカリ
yahoo_mall = ヤフオク店
; 楽天の発送待ち注文を何日前までさかのぼって探すか（最大62）
rakuten_days = 30
"""


# ---------------------------------------------------------------- 共通

def load_config():
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(DEFAULT_CONFIG, encoding="utf-8")
    c = configparser.ConfigParser()
    c.read(CONFIG_FILE, encoding="utf-8")
    s = c["SETTINGS"] if c.has_section("SETTINGS") else {}
    rk = c["RAKUTEN_API"] if c.has_section("RAKUTEN_API") else {}
    secret, key = rk.get("service_secret", "").strip(), rk.get("license_key", "").strip()
    other = rk.get("config_file", "").strip()
    if (not secret or not key) and other and Path(other).exists():
        o = configparser.ConfigParser()
        o.read(other, encoding="utf-8")
        if o.has_section("RAKUTEN_API"):
            secret, key = o["RAKUTEN_API"].get("service_secret", "").strip(), o["RAKUTEN_API"].get("license_key", "").strip()
    out = Path(s.get("output_dir", "出荷データ"))
    return {
        "rakuten_secret": secret, "rakuten_key": key,
        "output_dir": out if out.is_absolute() else APP_DIR / out,
        "status": s.get("status", "本日発送分"), "carrier": s.get("carrier", "日本郵便"),
        "mall": {"rakuten": s.get("rakuten_mall", "楽天市場店"), "amazon": s.get("amazon_mall", "Amazon店"),
                 "mercari": s.get("mercari_mall", "メルカリ"), "yahoo": s.get("yahoo_mall", "ヤフオク店")},
        "rakuten_days": min(int(s.get("rakuten_days", "30") or 30), 62),
    }


def load_history():
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    return {"since": "", "orders": {}}


def save_history(h):
    tmp = HISTORY_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(h, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(HISTORY_FILE)


def read_table(path):
    """CSV / TSV を文字コード（UTF-8 / Shift_JIS）と区切り文字を自動判定して [dict] で返す。"""
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "cp932"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("cp932", errors="replace")
    first = text.split("\n", 1)[0]
    delim = "\t" if first.count("\t") > first.count(",") else ","
    rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    if not rows:
        return [], []
    header = [h.strip().lstrip("﻿") for h in rows[0]]
    return header, [dict(zip(header, r)) for r in rows[1:] if any(c.strip() for c in r)]


def num(v):
    try:
        return int(round(float(str(v).replace(",", "").replace("¥", "").replace("￥", "").strip() or 0)))
    except ValueError:
        return 0


def zip7(v):
    return re.sub(r"[^0-9]", "", str(v or ""))


def join(*parts, sep=" "):
    return sep.join(p.strip() for p in parts if p and str(p).strip())


def parse_dt(s):
    """各モールの日時表記を日本時間の datetime にする。読めなければ None。"""
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                "%Y年%m月%d日 %H:%M:%S", "%Y年%m月%d日 %H:%M", "%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=JST)
        except ValueError:
            pass
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})([+-]\d{2})(\d{2})$", s)  # 楽天: +0900
        if not m:
            return None
        d = datetime.fromisoformat(f"{m.group(1)}{m.group(2)}:{m.group(3)}")
    return d.astimezone(JST) if d.tzinfo else d.replace(tzinfo=JST)


def fmt_dt(d):
    return d.strftime("%Y-%m-%d %H:%M:%S") if d else ""


def fmt_day(d):
    return d.strftime("%Y-%m-%d 00:00:00") if d else ""


def new_order(mall, number, ordered):
    o = {h: "" for h in HEADER}
    o.update({"モール": mall, "注文番号": number, "受注日": fmt_dt(ordered), "国": "日本", "お届け先：国": "日本",
              "小計": 0, "値引き": 0, "消費税": 0, "送料": 0, "ラッピング１料金": 0, "ラッピング２料金": 0,
              "代引き手数料": 0, "使用ポイント": 0, "合計": 0, "請求合計": 0, "ship-service-level": "Standard"})
    return {"head": o, "items": [], "ordered": ordered}


def set_buyer(o, name, kana, mail, pref, zipc, city, addr, tel):
    o["head"].update({"お名前": name, "お名前(フリガナ)": kana, "メールアドレス": mail, "都道府県": pref,
                      "郵便番号": zip7(zipc), "市区郡": city, "町名・番地・建物名": addr, "TEL": tel})


def set_dest(o, name, kana, tel, pref, zipc, city, addr):
    o["head"].update({"お届け先：お名前": name, "お届け先：お名前(フリガナ)": kana, "お届け先：TEL": tel,
                      "お届け先：都道府県": pref, "お届け先：郵便番号": zip7(zipc), "お届け先：市区郡": city,
                      "お届け先：町名・番地・建物名": addr})


# ---------------------------------------------------------------- 楽天（API）

RAKUTEN_TERM = {"0": "", "1": "午前中", "2": "午後", "9": "その他"}


def rakuten_term(v):
    v = str(v or "").strip()
    if v in RAKUTEN_TERM:
        return RAKUTEN_TERM[v]
    if re.fullmatch(r"\d{4}", v):
        return f"{int(v[:2])}時-{int(v[2:])}時"
    return v


def fetch_rakuten(cfg, since, log):
    if not cfg["rakuten_secret"] or not cfg["rakuten_key"]:
        log("楽天: shipping_export.ini に API のキーが無いため取得しません")
        return []
    token = base64.b64encode(f"{cfg['rakuten_secret']}:{cfg['rakuten_key']}".encode()).decode()
    h = {"Authorization": f"ESA {token}", "Content-Type": "application/json; charset=UTF-8"}
    now = datetime.now(JST)
    start = max(since, now - timedelta(days=cfg["rakuten_days"]))
    numbers, page = [], 1
    while True:
        body = {"version": 7, "dateType": 1, "orderProgressList": [300],
                "startDatetime": start.strftime("%Y-%m-%dT%H:%M:%S%z"), "endDatetime": now.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "PaginationRequestModel": {"requestRecordsAmount": 1000, "requestPage": page}}
        r = requests.post("https://api.rms.rakuten.co.jp/es/2.0/order/searchOrder/", headers=h, json=body, timeout=60)
        if r.status_code != 200:
            raise RuntimeError(f"楽天 searchOrder エラー HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        if any(m.get("messageType") == "ERROR" for m in data.get("MessageModelList") or []):
            raise RuntimeError(f"楽天 searchOrder エラー: {data['MessageModelList']}")
        numbers += data.get("orderNumberList") or []
        if page >= int((data.get("PaginationResponseModel") or {}).get("totalPages", 1) or 1):
            break
        page += 1
    log(f"楽天: 発送待ちの注文 {len(numbers)}件（{start:%m/%d %H:%M} 以降の注文）")
    orders = []
    for i in range(0, len(numbers), 100):
        r = requests.post("https://api.rms.rakuten.co.jp/es/2.0/order/getOrder/", headers=h,
                          json={"orderNumberList": numbers[i:i + 100], "version": 7}, timeout=60)
        if r.status_code != 200:
            raise RuntimeError(f"楽天 getOrder エラー HTTP {r.status_code}: {r.text[:300]}")
        for m in r.json().get("OrderModelList", []):
            orders.extend(rakuten_order(cfg, m))
    return orders


def rakuten_order(cfg, m):
    ordered = parse_dt(m.get("orderDatetime"))
    b = m.get("OrdererModel") or {}
    st = m.get("SettlementModel") or {}
    tel = lambda x: join(x.get("phoneNumber1"), x.get("phoneNumber2"), x.get("phoneNumber3"), sep="-")
    result = []
    packages = [p for p in m.get("PackageModelList") or [] if str(p.get("packageDeleteFlag", 0)) != "1"]
    for n, p in enumerate(packages):
        o = new_order(cfg["mall"]["rakuten"], m.get("orderNumber", ""), ordered)
        set_buyer(o, join(b.get("familyName"), b.get("firstName")), join(b.get("familyNameKana"), b.get("firstNameKana")),
                  b.get("emailAddress", ""), b.get("prefecture", ""), f"{b.get('zipCode1', '')}{b.get('zipCode2', '')}",
                  b.get("city", ""), b.get("subAddress", ""), tel(b))
        s = p.get("SenderModel") or {}
        set_dest(o, join(s.get("familyName"), s.get("firstName")), join(s.get("familyNameKana"), s.get("firstNameKana")),
                 tel(s), s.get("prefecture", ""), f"{s.get('zipCode1', '')}{s.get('zipCode2', '')}", s.get("city", ""),
                 s.get("subAddress", ""))
        first = n == 0  # 金額は注文単位なので、送付先が複数あるときは最初の送付先にだけ入れる
        o["head"].update({
            "お支払方法": st.get("settlementMethod", ""), "お届け日指定": m.get("deliveryDate") or "",
            "お届け時間帯指定": rakuten_term(m.get("shippingTerm")), "備考": (m.get("remarks") or "").strip(),
            "小計": num(m.get("goodsPrice")) if first else 0, "消費税": num(m.get("goodsTax")) if first else 0,
            "送料": num(m.get("postagePrice")) if first else 0, "代引き手数料": num(m.get("paymentCharge")) if first else 0,
            "値引き": num(m.get("couponAllTotalPrice")) if first else 0,
            "使用ポイント": num((m.get("PointModel") or {}).get("usedPoint")) if first else 0,
            "合計": num(m.get("totalPrice")) if first else 0, "請求合計": num(m.get("requestPrice")) if first else 0,
        })
        for it in p.get("ItemModelList") or []:
            if str(it.get("deleteItemFlag", 0)) == "1":
                continue
            o["items"].append((it.get("itemName", ""), it.get("itemNumber") or (it.get("manageNumber") or "").upper(),
                               num(it.get("price")), num(it.get("units")) or 1))
        if o["items"]:
            result.append(o)
    return result


# ---------------------------------------------------------------- 注文ファイル

def before(value, since):
    d = parse_dt(value)
    return bool(d and d < since)


def detect_mall(header):
    hs = {h.lower() for h in header}
    if "order-id" in hs and "sku" in hs:
        return "amazon"
    if "order_id" in hs and "original_product_id" in hs:
        return "mercari"
    if "orderid" in hs and ("yahooauctionmerchantid" in hs or "shipname" in hs):
        return "yahoo"
    return None


def read_amazon(cfg, rows, log, since):
    g = lambda r, *ks: next((r[k] for k in ks if k in r and r[k] is not None), "")
    orders = {}
    for r in rows:
        oid = g(r, "order-id")
        qty = num(g(r, "quantity-to-ship")) if "quantity-to-ship" in r else num(g(r, "quantity-purchased"))
        if not oid or qty <= 0 or before(g(r, "purchase-date"), since):
            continue
        if oid not in orders:
            o = new_order(cfg["mall"]["amazon"], oid, parse_dt(g(r, "purchase-date")))
            set_buyer(o, g(r, "buyer-name"), "", g(r, "buyer-email"), g(r, "ship-state"), g(r, "ship-postal-code"),
                      g(r, "ship-city"), join(g(r, "ship-address-1"), g(r, "ship-address-2"), g(r, "ship-address-3")),
                      g(r, "buyer-phone-number"))
            set_dest(o, g(r, "recipient-name"), "", g(r, "ship-phone-number"), g(r, "ship-state"), g(r, "ship-postal-code"),
                     g(r, "ship-city"), join(g(r, "ship-address-1"), g(r, "ship-address-2"), g(r, "ship-address-3")))
            o["head"].update({"入金日": fmt_day(parse_dt(g(r, "payments-date"))),
                              "ship-service-level": g(r, "ship-service-level") or "Standard",
                              "お届け日指定": g(r, "delivery-start-date")[:10], "お届け時間帯指定": g(r, "delivery-time-zone")})
            orders[oid] = o
        o = orders[oid]
        price = num(g(r, "item-price"))  # 数量分の合計金額
        o["items"].append((g(r, "product-name"), g(r, "sku"), price // qty if qty else price, qty))
        disc = num(g(r, "item-promotion-discount")) + num(g(r, "ship-promotion-discount"))
        o["head"]["小計"] += price
        o["head"]["送料"] += num(g(r, "shipping-price"))
        o["head"]["値引き"] += abs(disc)
    for o in orders.values():
        t = o["head"]["小計"] + o["head"]["送料"] - o["head"]["値引き"]
        o["head"].update({"合計": t, "請求合計": t})
    return list(orders.values())


def read_mercari(cfg, rows, log, since):
    orders, no_addr = {}, []
    for r in rows:
        oid = r.get("order_id", "").strip()
        if not oid or before(r.get("purchase_date"), since):
            continue
        if not (r.get("shipping_postal_code", "").strip() and r.get("shipping_address_1", "").strip()):
            no_addr.append(oid)
            continue
        qty = num(r.get("quantity")) or 1
        if oid not in orders:
            o = new_order(cfg["mall"]["mercari"], oid, parse_dt(r.get("purchase_date")))
            set_buyer(o, r.get("billing_name") or r.get("buyer_name", ""), "", "", r.get("billing_state", ""),
                      r.get("billing_postal_code", ""), r.get("billing_city", ""),
                      join(r.get("billing_address_1"), r.get("billing_address_2")), "")
            set_dest(o, r.get("shipping_name", ""), "", r.get("shipping_phone_number", ""), r.get("shipping_state", ""),
                     r.get("shipping_postal_code", ""), r.get("shipping_city", ""),
                     join(r.get("shipping_address_1"), r.get("shipping_address_2")))
            o["head"].update({"入金日": fmt_day(parse_dt(r.get("payment_date"))),
                              "お届け日指定": r.get("requested_delivery_date", ""),
                              "お届け時間帯指定": r.get("requested_delivery_time", ""),
                              "値引き": num(r.get("coupon_discount_amount"))})
            orders[oid] = o
        o = orders[oid]
        price = num(r.get("product_price"))
        o["items"].append((r.get("product_name", ""), r.get("original_product_id", "").strip(), price, qty))
        o["head"]["小計"] += price * qty
        o["head"]["送料"] += num(r.get("shipping_price"))
    for o in orders.values():
        t = o["head"]["小計"] + o["head"]["送料"] - o["head"]["値引き"]
        o["head"].update({"合計": t, "請求合計": t})
    if no_addr:
        log(f"メルカリ: お届け先の住所が無い注文 {len(no_addr)}件は出力しません（匿名配送など。手作業で確認）: {', '.join(no_addr[:10])}")
    return list(orders.values())


LINE_VALUE = re.compile(r"L(\d+)=(.*?)(?=&L\d+=|$)", re.S)  # 明細は "L1=…&L2=…" の形
SKU_IN_TEXT = re.compile(r"(?<![0-9A-Za-z])(\d{6}(?:[A-Za-z]{2}\d{6}|[A-Za-z]\d{7}))(?![0-9A-Za-z])")


def line_values(v):
    """ヤフオクの明細項目（例 "L1=2112" や "L1=…;L2=…"）を [値, ...] にする。L番号が無ければ [v]。"""
    v = (v or "").strip()
    found = LINE_VALUE.findall(v)
    if not found:
        return [v]
    return [x.strip() for _, x in sorted(found, key=lambda t: int(t[0]))]


def read_yahoo(cfg, rows, log, since):
    orders, unpaid, no_sku = {}, set(), []
    for r in rows:
        oid = r.get("OrderId", "").strip()
        if not oid or before(r.get("OrderTime"), since):
            continue
        if r.get("PayStatus", "1").strip() not in ("1", ""):
            unpaid.add(oid)  # 未入金は出荷しない
            continue
        if oid not in orders:
            o = new_order(cfg["mall"]["yahoo"], r.get("YahooAuctionId", "").strip() or oid, parse_dt(r.get("OrderTime")))
            set_buyer(o, r.get("BillName", ""), r.get("BillNameKana", ""), r.get("BillMailAddress", ""), r.get("BillPrefecture", ""),
                      r.get("BillZipCode", ""), r.get("BillCity", ""), join(r.get("BillAddress1"), r.get("BillAddress2")),
                      r.get("BillPhoneNumber", ""))
            set_dest(o, r.get("ShipName", ""), r.get("ShipNameKana", ""), r.get("ShipPhoneNumber", ""), r.get("ShipPrefecture", ""),
                     r.get("ShipZipCode", ""), r.get("ShipCity", ""), join(r.get("ShipAddress1"), r.get("ShipAddress2")))
            # Total = 支払額（モールクーポン適用後）、TotalPrice = 適用前。TotalMallCouponDiscount はマイナスで入る
            o["head"].update({"お支払方法": r.get("PayMethodName", ""), "備考": r.get("BuyerComments", "").strip(),
                              "送料": num(r.get("ShipCharge")), "代引き手数料": num(r.get("PayCharge")),
                              "値引き": abs(num(r.get("TotalMallCouponDiscount"))), "合計": num(r.get("Total")),
                              "請求合計": num(r.get("Total"))})
            orders[oid] = o
        o = orders[oid]
        titles, skus, subs = (line_values(r.get("Title")), line_values(r.get("YahooAuctionMerchantId")),
                              line_values(r.get("LineSubTotal")))
        for i in range(max(len(titles), len(skus), len(subs))):
            title = titles[i] if i < len(titles) else ""
            sku = skus[i] if i < len(skus) else ""
            price = num(subs[i]) if i < len(subs) else 0
            if not sku:
                # 複数商品の注文は商品コード（D列）が1つ目の分しか無い。タイトルに商品番号があれば使う
                found = SKU_IN_TEXT.findall(title)
                sku = found[-1].upper() if found else ""
            if not sku:
                no_sku.append(f"{oid}（{i + 1}つ目の商品）")
            o["items"].append((title, sku, price, 1))
            o["head"]["小計"] += price
    if unpaid:
        log(f"ヤフオク: 未入金の注文 {len(unpaid)}件は出力しません: {', '.join(sorted(unpaid)[:10])}")
    if no_sku:
        log(f"ヤフオク: 商品コードが無い明細 {len(no_sku)}件があります（そのまま出力。手作業で確認）: {', '.join(no_sku[:10])}")
    return list(orders.values())


READERS = {"amazon": read_amazon, "mercari": read_mercari, "yahoo": read_yahoo}
MALL_JA = {"rakuten": "楽天", "amazon": "Amazon", "mercari": "メルカリ", "yahoo": "ヤフオク"}


# ---------------------------------------------------------------- 出力

def q(v):
    s = str(v)
    if any(c in s for c in ' ,"\r\n　\t'):
        return '"' + s.replace('"', '""') + '"'
    return s


def write_csv(cfg, orders):
    cfg["output_dir"].mkdir(parents=True, exist_ok=True)
    path = cfg["output_dir"] / f"order-{datetime.now(JST):%Y%m%d%H%M%S}.csv"
    lines = [",".join(q(h) for h in HEADER)]
    for o in orders:
        for name, code, price, qty in o["items"]:
            row = dict(o["head"])
            row.update({"ステータス": cfg["status"], "お届け先：配送業者": cfg["carrier"],
                        "商品名": name, "商品コード": code, "商品金額": price, "商品数": qty})
            lines.append(",".join(q(row[h]) for h in HEADER))
    path.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    return path


def create(cfg, since, files, use_rakuten, skip_done, log):
    """注文を集めて出荷データを作る。戻り値は作ったファイル（無ければ None）。"""
    history = load_history()
    collected = []
    if use_rakuten:
        collected += [("rakuten", o) for o in fetch_rakuten(cfg, since, log)]
    for f in files:
        header, rows = read_table(f)
        mall = detect_mall(header)
        if not mall:
            log(f"× {Path(f).name}: どのモールの注文ファイルか分かりません（先頭の列: {', '.join(header[:6])}）")
            continue
        got = READERS[mall](cfg, rows, log, since)
        log(f"{MALL_JA[mall]}: {Path(f).name} から注文 {len(got)}件（{since:%m/%d %H:%M} 以降）")
        collected += [(mall, o) for o in got]

    out, old, done, seen = [], 0, 0, set()
    for mall, o in collected:
        key = f"{mall}:{o['head']['注文番号']}"
        if key in seen:
            continue
        seen.add(key)
        if o["ordered"] and o["ordered"] < since:
            old += 1
            continue
        if skip_done and key in history["orders"]:
            done += 1
            continue
        out.append((key, o))
    log(f"対象の注文: {len(out)}件（{since:%m/%d %H:%M} より前で除外 {old}件 / 出力済みで除外 {done}件）")
    history["since"] = since.strftime("%Y-%m-%d %H:%M")
    if not out:
        save_history(history)
        log("出力する注文はありません。")
        return None
    out.sort(key=lambda x: (x[1]["ordered"] or datetime.min.replace(tzinfo=JST)))
    path = write_csv(cfg, [o for _, o in out])
    stamp = datetime.now(JST).isoformat(timespec="seconds")
    for key, _ in out:
        history["orders"][key] = stamp
    save_history(history)
    by = {}
    for key, o in out:
        by[key.split(":")[0]] = by.get(key.split(":")[0], 0) + 1
    log(f"作成しました: {path.name}（注文 {len(out)}件 / 行数 {sum(len(o['items']) for _, o in out)}）  "
        + " / ".join(f"{MALL_JA[m]} {n}件" for m, n in by.items()))
    return path


# ---------------------------------------------------------------- 画面

def main_gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext

    cfg = load_config()
    history = load_history()
    root = tk.Tk()
    root.title(APP_NAME)
    root.geometry("760x560")
    files = []

    frm = tk.Frame(root, padx=10, pady=8)
    frm.pack(fill="x")
    tk.Label(frm, text="この日時以降の注文を対象にする（例 2026-10-21 23:00）").grid(row=0, column=0, sticky="w")
    since_var = tk.StringVar(value=history.get("since", ""))
    tk.Entry(frm, textvariable=since_var, width=20).grid(row=0, column=1, sticky="w", padx=6)

    rak_var = tk.BooleanVar(value=bool(cfg["rakuten_secret"] and cfg["rakuten_key"]))
    tk.Checkbutton(frm, text="楽天の発送待ち注文を API で取得する", variable=rak_var).grid(row=1, column=0, columnspan=2, sticky="w")
    skip_var = tk.BooleanVar(value=True)
    tk.Checkbutton(frm, text="出力済みの注文は除く（二重出荷を防ぐ）", variable=skip_var).grid(row=2, column=0, columnspan=2, sticky="w")

    tk.Label(root, text="注文ファイル（Amazon の注文レポート・メルカリ・ヤフオクの注文CSV）", padx=10).pack(anchor="w")
    lst = tk.Listbox(root, height=6)
    lst.pack(fill="x", padx=10)

    def add_files():
        for f in filedialog.askopenfilenames(title="注文ファイルを選ぶ", filetypes=[("注文ファイル", "*.csv *.txt *.tsv"), ("すべて", "*.*")]):
            if f not in files:
                header, _ = read_table(f)
                mall = detect_mall(header)
                files.append(f)
                lst.insert("end", f"[{MALL_JA.get(mall, '不明')}] {f}")

    def remove_files():
        for i in reversed(lst.curselection()):
            lst.delete(i)
            files.pop(i)

    bf = tk.Frame(root, padx=10, pady=4)
    bf.pack(fill="x")
    tk.Button(bf, text="注文ファイルを追加…", command=add_files).pack(side="left")
    tk.Button(bf, text="選んだファイルを外す", command=remove_files).pack(side="left", padx=6)
    run_btn = tk.Button(bf, text="出荷データを作成", bg="#2d6cdf", fg="white", padx=12)
    run_btn.pack(side="right")
    tk.Button(bf, text="出力フォルダを開く", command=lambda: (cfg["output_dir"].mkdir(parents=True, exist_ok=True),
                                                         os.startfile(cfg["output_dir"]))).pack(side="right", padx=6)

    logbox = scrolledtext.ScrolledText(root, height=16, state="disabled")
    logbox.pack(fill="both", expand=True, padx=10, pady=(4, 10))

    def log(msg):
        def put():
            logbox.configure(state="normal")
            logbox.insert("end", f"{datetime.now():%H:%M:%S}  {msg}\n")
            logbox.see("end")
            logbox.configure(state="disabled")
        root.after(0, put)

    def run():
        since = parse_dt(since_var.get())
        if not since:
            messagebox.showerror(APP_NAME, "「この日時以降の注文」を 2026-10-21 23:00 のように入力してください")
            return
        if not rak_var.get() and not files:
            messagebox.showerror(APP_NAME, "楽天の取得にチェックを入れるか、注文ファイルを追加してください")
            return
        run_btn.configure(state="disabled")

        def work():
            try:
                path = create(cfg, since, list(files), rak_var.get(), skip_var.get(), log)
                if path:
                    root.after(0, lambda: messagebox.showinfo(APP_NAME, f"出荷データを作成しました。\n{path}"))
            except Exception as e:
                log(f"エラー: {e}")
                log(traceback.format_exc())
                root.after(0, lambda: messagebox.showerror(APP_NAME, f"エラーが発生しました。\n{e}"))
            finally:
                root.after(0, lambda: run_btn.configure(state="normal"))

        threading.Thread(target=work, daemon=True).start()

    run_btn.configure(command=run)
    log(f"設定ファイル: {CONFIG_FILE}")
    log(f"出力先: {cfg['output_dir']}")
    if not (cfg["rakuten_secret"] and cfg["rakuten_key"]):
        log("楽天の API キーが未設定です（shipping_export.ini）。楽天は取得しません")
    root.mainloop()


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        # 起動確認用（画面を出さない）: 設定を読み、結果を selftest.txt に書く
        import tkinter  # noqa: F401  exe に画面部品が入っているかの確認
        c = load_config()
        (APP_DIR / "selftest.txt").write_text(
            f"ok\nconfig={CONFIG_FILE}\nrakuten_key={'set' if c['rakuten_key'] else 'empty'}\noutput={c['output_dir']}\n", encoding="utf-8")
        sys.exit(0)
    main_gui()
