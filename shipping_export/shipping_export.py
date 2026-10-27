# -*- coding: utf-8 -*-
"""
出荷データ作成アプリ（ワサビスイッチ停止中の代替）

各モールの「出荷できる注文」を、ワサビスイッチが出力していた注文CSV（order-YYYYMMDDHHMMSS.csv と同じ65列・UTF-8）に
まとめて出力する。出力したファイルを出荷システムに取り込んでピッキング・梱包を行う。

  楽天     : RMS 受注API で「発送待ち（進捗300）」の注文を取得
  Amazon   : セラーセントラルの注文レポート（未出荷の注文）ファイル
  メルカリ : メルカリShopsの注文CSV（orders_*.csv）
  ヤフオク : ヤフオクの注文CSV（YahooAuctionId, OrderId, ... の形式）。入金済み（PayStatus=1）だけ出力
  ヤフオク1: 個人アカウントの取引ナビのページの文字（Ctrl+A → Ctrl+C →「取引ナビを貼り付け」）。発送連絡の前の取引だけ

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
import time
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
; ヤフーの注文のうちオークションIDが無いもの（ショッピングの注文）のモール名
yahoo_shopping_mall = ヤフーショッピング店
; 個人のヤフオクアカウント（取引ナビの貼り付け）
auction_mall = ヤフオク1
; 楽天の発送待ち注文を何日前までさかのぼって探すか（最大62）
rakuten_days = 30
; 画面の「注文日がこの日時以降」の初期値（何日前か）。出荷済みリストで除外するので広めでよい
default_days = 14
; ヤフオク店の注文番号の頭に付ける文字（ワサビの注文番号は「ストアID-注文番号」）
yahoo_order_prefix = vivoage-

[AMAZON_SP_API]
; Amazon の注文がキャンセル・発送済みでないかを確認するためのキー（空なら確認しない）
client_id =
client_secret =
refresh_token =
marketplace_id = A1VC38T7YXB528
endpoint = https://sellingpartnerapi-fe.amazon.com
; 既存の設定ファイル（[AMAZON_SP_API] がある ini）を使う場合はそのパス。キーが空のときだけ使う
config_file =

[WASABI]
; ヤフオク店のまとめて取引の2つ目以降など、商品コードが分からない商品を、ワサビスイッチの商品名から探すためのトークン
; （KEY:SECRET。空なら探さない）
token =
base_url = https://world-switch.com/kaitoriouji/ext
; 既存の設定ファイル（[WASABI] がある wasabi_config.ini）を使う場合はそのパス。token が空のときだけ使う
config_file =
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
    amz = {k: "" for k in ("client_id", "client_secret", "refresh_token")}
    a = c["AMAZON_SP_API"] if c.has_section("AMAZON_SP_API") else {}
    amz.update({k: a.get(k, "").strip() for k in amz})
    amz["marketplace_id"] = a.get("marketplace_id", "A1VC38T7YXB528").strip() or "A1VC38T7YXB528"
    amz["endpoint"] = (a.get("endpoint", "") or "https://sellingpartnerapi-fe.amazon.com").strip().rstrip("/")
    other = a.get("config_file", "").strip() if a else ""
    if not all(amz[k] for k in ("client_id", "client_secret", "refresh_token")) and other and Path(other).exists():
        o = configparser.ConfigParser()
        o.read(other, encoding="utf-8")
        if o.has_section("AMAZON_SP_API"):
            for k in ("client_id", "client_secret", "refresh_token", "marketplace_id", "endpoint"):
                v = o["AMAZON_SP_API"].get(k, "").strip()
                if v and "ここに" not in v:
                    amz[k] = v.rstrip("/") if k == "endpoint" else v
    w = c["WASABI"] if c.has_section("WASABI") else {}
    ws = {"token": w.get("token", "").strip(),
          "base_url": (w.get("base_url", "") or "https://world-switch.com/kaitoriouji/ext").strip().rstrip("/")}
    other = w.get("config_file", "").strip() if w else ""
    if not ws["token"] and other and Path(other).exists():
        o = configparser.ConfigParser()
        o.read(other, encoding="utf-8")
        if o.has_section("WASABI"):
            ws["token"] = o["WASABI"].get("token", "").strip()
            ws["base_url"] = (o["WASABI"].get("base_url", "") or ws["base_url"]).strip().rstrip("/")
    return {
        "amazon": amz if all(amz[k] for k in ("client_id", "client_secret", "refresh_token")) else None,
        "wasabi": ws if ws["token"] and "ここに" not in ws["token"] else None,
        "default_days": int(s.get("default_days", "14") or 14),
        "yahoo_prefix": s.get("yahoo_order_prefix", "vivoage-"),
        "rakuten_secret": secret, "rakuten_key": key,
        "output_dir": out if out.is_absolute() else APP_DIR / out,
        "status": s.get("status", "本日発送分"), "carrier": s.get("carrier", "日本郵便"),
        "mall": {"rakuten": s.get("rakuten_mall", "楽天市場店"), "amazon": s.get("amazon_mall", "Amazon店"),
                 "mercari": s.get("mercari_mall", "メルカリ"), "yahoo": s.get("yahoo_mall", "ヤフオク店"), "yahoo_shopping": s.get("yahoo_shopping_mall", "ヤフーショッピング店"),
                 "auction": s.get("auction_mall", "ヤフオク1")},
        "rakuten_days": min(int(s.get("rakuten_days", "30") or 30), 62),
    }


def load_history():
    """出力履歴。orders = {"モール:注文番号": 出力日時}、skus = {商品コード: {mall, order, ordered, at}}"""
    h = json.loads(HISTORY_FILE.read_text(encoding="utf-8")) if HISTORY_FILE.exists() else {}
    h.setdefault("orders", {})
    h.setdefault("skus", {})
    return h


def save_history(h):
    tmp = HISTORY_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(h, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(HISTORY_FILE)


def read_text(path):
    """文字コード（UTF-8 / Shift_JIS）を自動判定してファイルの文字を返す。"""
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "cp932"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("cp932", errors="replace")


def read_table(path):
    """CSV / TSV を文字コード（UTF-8 / Shift_JIS）と区切り文字を自動判定して [dict] で返す。"""
    text = read_text(path)
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
        oid = re.sub(r"^order_", "", r.get("order_id", "").strip())  # ワサビの注文番号は order_ を付けない
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


def is_yahoo_items(header):
    """ストアクリエイターProの注文データの明細ファイル（default_all_items.csv）かどうか。"""
    return "Order ID" in header and "Line ID" in header and "Product Code" in header


def read_yahoo_items(rows):
    """明細ファイルを {(注文番号, 明細番号): (Product Code, Description)} にする。"""
    return {(r.get("Order ID", "").strip(), int(num(r.get("Line ID")))): (r.get("Product Code", "").strip(), r.get("Description", ""))
            for r in rows if r.get("Order ID", "").strip()}


def yahoo_item_code(item, title):
    """明細の商品コード。ショッピングの注文は Product Code が商品番号。オークションは AUCTION+オークションID なので、
    そのときは商品名（Description）の中の商品番号を使う。分からなければ ""。"""
    if not item:
        return ""
    code, desc = item
    if code and not code.upper().startswith("AUCTION"):
        return code.upper()
    found = SKU_IN_TEXT.findall(desc or "")
    return found[-1].upper() if found else ""


def read_yahoo(cfg, rows, log, since):
    orders, unpaid = {}, set()
    for r in rows:
        oid = r.get("OrderId", "").strip()
        if not oid or before(r.get("OrderTime"), since):
            continue
        if r.get("PayStatus", "1").strip() not in ("1", ""):
            unpaid.add(oid)  # 未入金は出荷しない
            continue
        if oid not in orders:
            # ワサビの注文番号は「ストアID-注文番号」（例 vivoage-10215030）
            # オークションIDが無いのはヤフーショッピングの注文（ワサビでは別のモール）
            mall_name = cfg["mall"]["yahoo"] if r.get("YahooAuctionId", "").strip() else cfg["mall"]["yahoo_shopping"]
            o = new_order(mall_name, f"{cfg['yahoo_prefix']}{oid}", parse_dt(r.get("OrderTime")))
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
                # ショッピングの注文は商品コード（D列）が空。明細ファイル（default_all_items）があればその商品コードを使う
                sku = yahoo_item_code(cfg.get("yahoo_items", {}).get((oid, i + 1)), title)
            if not sku:
                # 複数商品の注文は商品コード（D列）が1つ目の分しか無い。タイトルに商品番号があれば使う
                # （無ければ出荷データを作るときに fill_missing_skus でワサビの商品名から探す）
                found = SKU_IN_TEXT.findall(title)
                sku = found[-1].upper() if found else ""
            o["items"].append((title, sku, price, 1))
            o["head"]["小計"] += price
    if unpaid:
        log(f"ヤフー: 未入金の注文 {len(unpaid)}件は出力しません: {', '.join(sorted(unpaid)[:10])}")
    return list(orders.values())


# ---------------------------------------------------------------- ワサビスイッチ（商品コードが分からない商品を商品名から探す）

TITLE_CODE = re.compile(r"^([A-Za-z]\d{3}-(\d{4})-\d{2})(?=\s)")  # タイトル先頭の管理コード（例 m543-0011-15）


def ws_search(ws, query):
    """ワサビの商品検索。該当した商品番号のリストを返す。"""
    r = requests.post(f"{ws['base_url']}/api/products/search", timeout=60, json={
        "sort": "product_id", "reverse": False, "page": 1, "limit": 10, "query": query},
        headers={"Authorization": f"Bearer {ws['token']}", "Content-Type": "application/json", "Accept": "application/json"})
    time.sleep(0.4)  # 速度制限（約3リクエスト/秒）を守る
    if r.status_code != 200:
        raise RuntimeError(f"ワサビの商品検索 HTTP {r.status_code}")
    return list(dict.fromkeys(str(d.get("商品番号") or "").strip() for d in r.json().get("data", []) if d.get("商品番号")))


def ws_find_sku(ws, title):
    """ヤフオクの商品タイトルからワサビの商品番号を探す。戻り値は (商品番号, 候補)。1件に決まらなければ商品番号は ""。
    ワサビの「ヤフー｜商品名」がヤフオクのタイトルと同じ。本・CDなどは「表示用商品名」から【中古】を除いたものがタイトル。"""
    title = title.strip()
    m = TITLE_CODE.match(title)
    tries = [{"ヤフー｜商品名": {"eq": title}}]
    if m:
        tries.append({"ヤフー｜商品名": {"starts": m.group(1)}})  # タイトルの空白などが少し違っても見つかるように
    tries += [{"表示用商品名": {"eq": "【中古】" + title}}, {"表示用商品名": {"eq": title}}]
    for q in tries:
        found = ws_search(ws, q)
        if m and len(found) > 1:
            # 管理コードの真ん中の4桁は商品番号の末尾4桁
            found = [s for s in found if s.endswith(m.group(2))] or found
        if len(found) == 1:
            return found[0], found
        if found:
            return "", found
    return "", []


def fill_missing_skus(cfg, orders, mall_ja, log):
    """商品コードが空の明細を、ワサビの商品名から探して埋める。見つからない・候補が複数のものは空のまま出力する。"""
    missing = [(o, i) for o in orders for i, it in enumerate(o["items"]) if not it[1]]
    if not missing:
        return
    label = lambda o, i: f"{o['head']['注文番号']}（{i + 1}つ目「{o['items'][i][0][:20]}」）"
    if not cfg["wasabi"]:
        log(f"{mall_ja}: 商品コードが無い明細 {len(missing)}件（ワサビのトークンが未設定のため探していません。手作業で確認）: "
            + ", ".join(label(o, i) for o, i in missing[:10]))
        return
    left, error = [], None
    for o, i in missing:
        title, _, price, qty = o["items"][i]
        sku, cands = "", []
        if not error:
            try:
                sku, cands = ws_find_sku(cfg["wasabi"], title)
            except Exception as e:  # 通信エラーなど。出荷データの作成は止めない
                error = e
        if sku:
            o["items"][i] = (title, sku, price, qty)
        else:
            left.append(label(o, i) + (f" 候補: {' / '.join(cands[:5])}" if cands else ""))
    if error:
        log(f"{mall_ja}: ワサビで商品コードを探す途中でエラーになりました（{type(error).__name__}: {str(error)[:80]}）")
    log(f"{mall_ja}: 商品コードが無い明細 {len(missing)}件をワサビの商品名で探しました → 見つかった {len(missing) - len(left)}件"
        + (f" / 見つからない・候補が複数 {len(left)}件（商品コードは空のまま出力。手作業で確認）" if left else ""))
    for s in left[:20]:
        log(f"   {s}")


# ---------------------------------------------------------------- ヤフオク1（個人アカウントの取引ナビ）

PREF = re.compile(r"北海道|東京都|(?:京都|大阪)府|.{2,3}県")
NAVI_LABELS = {"氏名", "住所", "電話番号", "配送方法", "お支払い情報", "支払い方法"}
NAVI_ITEM_LINE = re.compile(r"数量[：:]|落札価格[：:]|終了日時[：:]")
NAVI_AUCTION_ID = re.compile(r"オークションID[：:]\s*([0-9A-Za-z]+)")


def is_navi_text(text):
    return bool(NAVI_AUCTION_ID.search(text or ""))


def split_address(s):
    """「和歌山県 日高郡日高町 大字志賀 3473-1」→ (都道府県, 市区郡, 残り)。取引ナビは空白で区切って表示する。
    区切りが足りなければ都道府県だけ切り出し、残りは町名・番地に入れる。"""
    s = re.sub(r"\s+", " ", s.replace("　", " ")).strip()
    parts = s.split(" ", 2)
    if len(parts) == 3 and PREF.fullmatch(parts[0]):
        return parts[0], parts[1], parts[2]
    m = PREF.match(s)
    if not m:
        return "", "", s
    return m.group(0), "", s[m.end():].strip()


def navi_ended(text, now):
    """「終了日時：10月22日 20時10分」を日時にする。年は書かれていないので、未来の日付になるなら前年とする。"""
    m = re.search(r"終了日時[：:]\s*(\d{1,2})月(\d{1,2})日\s*(\d{1,2})時(\d{1,2})分", text)
    if not m:
        return None
    mo, d, h, mi = map(int, m.groups())
    for year in (now.year, now.year - 1):
        try:
            dt = datetime(year, mo, d, h, mi, tzinfo=JST)
        except ValueError:  # 2月29日など
            continue
        if dt <= now + timedelta(days=1):
            return dt
    return None


def parse_navi(cfg, text, now=None):
    """取引ナビのページの文字（Ctrl+A → Ctrl+C）から注文を作る。戻り値は (注文, None) か (None, 読めなかった理由)。
    注文番号はオークションID（まとめて取引なら1つ目）。「発送連絡をする」がある（入金済み・未発送の）取引だけ受け付ける。"""
    now = now or datetime.now(JST)
    lines = [l.strip() for l in (text or "").replace("\r", "").split("\n")]
    if not is_navi_text(text):
        return None, "オークションIDが見つかりません。取引ナビのページで Ctrl+A → Ctrl+C してください"
    if "発送連絡をする" not in text:
        return None, "「発送連絡をする」がありません。未入金か、発送連絡済みの取引です"
    start = next((i for i, l in enumerate(lines) if l.startswith("お届け情報")), None)
    if start is None:
        return None, "お届け情報がありません（匿名配送などでお届け先が出ていない取引は手作業で入力してください）"

    def field(label):
        """お届け情報より後で、ラベルの行の次から次のラベルまでの空でない行"""
        i = next((i for i in range(start, len(lines)) if lines[i] == label), None)
        vals = []
        for l in lines[i + 1:] if i is not None else []:
            if l in NAVI_LABELS or l.startswith("商品情報"):
                break
            if l:
                vals.append(l)
        return vals

    name = (field("氏名") or [""])[0]
    addr_lines = field("住所")
    m = re.match(r"〒?\s*(\d{3})-?(\d{4})\s*(.*)$", addr_lines[0]) if addr_lines else None
    if not name or not m:
        return None, "お届け先の氏名・郵便番号を読み取れませんでした。手作業で入力してください"
    zipc = m.group(1) + m.group(2)
    pref, city, addr = split_address(" ".join([m.group(3)] + addr_lines[1:]))
    if not pref or not addr:
        return None, "お届け先の住所を読み取れませんでした。手作業で入力してください"
    tel = (field("電話番号") or [""])[0]
    fee = re.search(r"送料[：:]\s*([\d,]+)\s*円", " ".join(field("配送方法")))
    pay = re.sub(r"[（(]受取明細[）)]", "", (field("支払い方法") or [""])[0]).strip()

    pi = next((i for i in range(start, len(lines)) if lines[i].startswith("商品情報")), None)
    items, block, aid, ended = [], [], "", None
    for l in lines[pi + 1:] if pi is not None else []:
        m = NAVI_AUCTION_ID.search(l)
        if not m:
            if l and l != "商品ページ":
                block.append(l)
            continue
        # 商品名・数量・落札価格・終了日時のあとに「オークションID」が来る。まとめて取引ならこれが商品ごとに続く
        btext = "\n".join(block)
        titles = [b for b in block if not NAVI_ITEM_LINE.search(b)]
        title = next((t for t in titles if SKU_IN_TEXT.search(t)), titles[0] if titles else "")
        found = SKU_IN_TEXT.findall(title)
        qty = re.search(r"数量[：:]\s*(\d+)", btext)
        price = re.search(r"落札価格[：:]\s*([\d,]+)\s*円", btext)
        if not price:
            return None, f"落札価格を読み取れませんでした（オークションID {m.group(1)}）"
        items.append((title, found[-1].upper() if found else "", num(price.group(1)), int(qty.group(1)) if qty else 1))
        aid = aid or m.group(1)
        ended = ended or navi_ended(btext, now)
        block = []
    if not items:
        return None, "商品情報（商品名・落札価格・オークションID）を読み取れませんでした"
    if not ended:
        return None, "終了日時を読み取れませんでした"

    o = new_order(cfg["mall"]["auction"], aid, ended)
    set_buyer(o, name, "", "", pref, zipc, city, addr, tel)
    set_dest(o, name, "", tel, pref, zipc, city, addr)
    sub = sum(p * q for _, _, p, q in items)
    ship = num(fee.group(1)) if fee else 0
    o["items"] = items
    o["head"].update({"お支払方法": pay, "入金日": fmt_day(ended), "小計": sub, "送料": ship,
                      "合計": sub + ship, "請求合計": sub + ship})
    return o, None


READERS = {"amazon": read_amazon, "mercari": read_mercari, "yahoo": read_yahoo}
MALL_JA = {"rakuten": "楽天", "amazon": "Amazon", "mercari": "メルカリ", "yahoo": "ヤフー", "auction": "ヤフオク1"}
# 同時刻の重複ではキャンセルの影響が大きいモールを優先して発送
MALL_PRIORITY = {"amazon": 0, "rakuten": 1, "mercari": 2, "yahoo": 3, "auction": 4}


def is_shipped_list(header):
    """ピッキングリスト（出荷済みの注文番号リスト。end-order.csv）かどうか。"""
    return "注文番号" in header and "発送日" in header and "商品コード" not in header


def read_shipped(rows):
    return {r.get("注文番号", "").strip() for r in rows if r.get("注文番号", "").strip() not in ("", "注文番号")}


# ---------------------------------------------------------------- Amazon（キャンセル・発送済みの確認）

def amazon_open_orders(cfg, created_after, log):
    """Amazon の「未発送（Unshipped / PartiallyShipped）」の自己発送注文の注文番号を返す。確認できなければ None。"""
    a = cfg["amazon"]
    if not a:
        log("Amazon: API のキーが無いため、キャンセル・発送済みの確認はしません（未出荷の注文レポートを使ってください）")
        return None
    t = requests.post("https://api.amazon.com/auth/o2/token", timeout=60, data={
        "grant_type": "refresh_token", "refresh_token": a["refresh_token"],
        "client_id": a["client_id"], "client_secret": a["client_secret"]})
    if t.status_code != 200:
        log(f"Amazon: 認証に失敗したため、キャンセルの確認はしません（HTTP {t.status_code}）")
        return None
    h = {"x-amz-access-token": t.json()["access_token"], "Accept": "application/json"}
    params = {"MarketplaceIds": a["marketplace_id"], "OrderStatuses": "Unshipped,PartiallyShipped",
              "FulfillmentChannels": "MFN", "MaxResultsPerPage": 100,
              "CreatedAfter": (created_after - timedelta(minutes=5)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    found = set()
    while True:
        for attempt in range(8):
            r = requests.get(f"{a['endpoint']}/orders/v0/orders", headers=h, params=params, timeout=60)
            if r.status_code not in (429, 500, 503):
                break
            time.sleep(min(60, 5 * (attempt + 1)))  # 注文の一覧は呼び出し回数の制限が厳しい
        if r.status_code != 200:
            log(f"Amazon: 注文の確認に失敗したため、キャンセルの確認はしません（HTTP {r.status_code}）")
            return None
        pl = r.json().get("payload", {})
        found.update(o["AmazonOrderId"] for o in pl.get("Orders", []))
        if not pl.get("NextToken"):
            return found
        params = {"MarketplaceIds": a["marketplace_id"], "NextToken": pl["NextToken"]}


# ---------------------------------------------------------------- 出力

def q(v):
    s = str(v)
    if any(c in s for c in ' ,"\r\n　\t'):
        return '"' + s.replace('"', '""') + '"'
    return s


def write_csv(cfg, orders, name):
    cfg["output_dir"].mkdir(parents=True, exist_ok=True)
    path = cfg["output_dir"] / name
    lines = [",".join(q(h) for h in HEADER)]
    for o in orders:
        for item_name, code, price, qty in o["items"]:
            row = dict(o["head"])
            row.update({"ステータス": cfg["status"], "お届け先：配送業者": cfg["carrier"],
                        "商品名": item_name, "商品コード": code, "商品金額": price, "商品数": qty})
            lines.append(",".join(q(row[h]) for h in HEADER))
    path.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    return path


def find_duplicates(out, history):
    """同じ商品コードが複数の注文（または前回までに出力した注文）にあるものを探す。
    先に注文された方を「発送」、それ以外を「キャンセル候補」とする。戻り値は (アラート行, キャンセル候補の注文キー集合)。"""
    by_sku = {}
    for key, mall, o in out:
        for _, code, _, _ in o["items"]:
            if code:
                by_sku.setdefault(code.upper(), {})[key] = (key, mall, o)  # 同じ注文の重複は1つにする
    alerts, losers = [], set()
    order_key = lambda x: (x[2]["ordered"] or datetime.max.replace(tzinfo=JST), MALL_PRIORITY.get(x[1], 9))
    for sku, found in by_sku.items():
        lst = sorted(found.values(), key=order_key)
        prev = history["skus"].get(sku)
        if prev and prev["order"] not in found:
            # 前回までに出荷データに出した注文と同じ商品（すでに出荷済みのはず）
            alerts.append([sku, "発送（出力済み）", prev["mall"], prev["order"].split(":", 1)[-1], prev.get("ordered", ""),
                           "前回までに出荷データに出力済み"])
            for k, m, o in lst:
                alerts.append([sku, "キャンセル候補", MALL_JA[m], o["head"]["注文番号"], o["head"]["受注日"], "前回出力した注文と同じ商品"])
                losers.add(k)
            continue
        if len(lst) < 2:
            continue
        win = lst[0]
        alerts.append([sku, "発送", MALL_JA[win[1]], win[2]["head"]["注文番号"], win[2]["head"]["受注日"], "先に注文された方"])
        for k, m, o in lst[1:]:
            alerts.append([sku, "キャンセル候補", MALL_JA[m], o["head"]["注文番号"], o["head"]["受注日"], "後から注文された方"])
            losers.add(k)
    return alerts, losers


def create(cfg, since, files, use_rakuten, skip_done, hold_dupes, log, navi=()):
    """注文を集めて出荷データを作る。navi は取引ナビから読み取ったヤフオク1の注文。
    戻り値は (作ったファイル or None, 重複アラート行)。"""
    history = load_history()
    collected, shipped, shipped_files = [], set(), 0
    if navi:
        log(f"ヤフオク1: 取引ナビから注文 {len(navi)}件")
        collected += [("auction", o) for o in navi]
    cfg["yahoo_items"] = {}
    order_files = []
    for f in files:
        header, rows = read_table(f)
        if "Order ID" in header and "Order Kind" in header:
            log(f"ヤフー注文データ: {Path(f).name} は使いません（注文は download.csv 形式、商品コードは明細ファイルから読みます）")
            continue
        if is_yahoo_items(header):
            got = read_yahoo_items(rows)
            cfg["yahoo_items"].update(got)
            log(f"ヤフー明細: {Path(f).name} から {len(got)}行（ヤフーの注文の商品コードを補うのに使います）")
            continue
        order_files.append((f, header, rows))
    for f, header, rows in order_files:
        if is_shipped_list(header):
            got = read_shipped(rows)
            shipped |= got
            shipped_files += 1
            log(f"出荷済みリスト: {Path(f).name} から注文番号 {len(got):,}件")
            continue
        mall = detect_mall(header)
        if not mall:
            log(f"× {Path(f).name}: どのモールの注文ファイルか分かりません（先頭の列: {', '.join(header[:6])}）")
            continue
        got = READERS[mall](cfg, rows, log, since)
        log(f"{MALL_JA[mall]}: {Path(f).name} から注文 {len(got)}件（{since:%m/%d %H:%M} 以降）")
        collected += [(mall, o) for o in got]
    if use_rakuten:
        collected += [("rakuten", o) for o in fetch_rakuten(cfg, since, log)]
    if not shipped_files:
        log("※ 出荷済みリスト（ピッキングリストの end-order.csv）が追加されていません。出荷済みの注文も出力されるおそれがあります")

    out, old, done, in_list, seen = [], 0, 0, 0, set()
    for mall, o in collected:
        key = f"{mall}:{o['head']['注文番号']}"
        if key in seen:
            continue
        seen.add(key)
        if o["ordered"] and o["ordered"] < since and mall != "auction":  # 取引ナビは貼り付けた分をすべて対象にする
            old += 1
        elif o["head"]["注文番号"] in shipped:
            in_list += 1
        elif skip_done and key in history["orders"]:
            done += 1
        else:
            out.append((key, mall, o))
    log(f"候補の注文: {len(out)}件（除外: 出荷済みリストにあり {in_list}件 / 出力済み {done}件 / {since:%m/%d %H:%M} より前 {old}件）")

    amz = [o for _, m, o in out if m == "amazon" and o["ordered"]]
    if amz:
        open_ids = amazon_open_orders(cfg, min(o["ordered"] for o in amz), log)
        if open_ids is not None:
            closed = {k for k, m, o in out if m == "amazon" and o["head"]["注文番号"] not in open_ids}
            out = [x for x in out if x[0] not in closed]
            log(f"Amazon: キャンセル・発送済み・保留中の注文 {len(closed)}件を除外しました"
                + (f"（例: {', '.join(sorted(k.split(':', 1)[1] for k in closed)[:5])}）" if closed else ""))

    # まとめて取引の2つ目以降など、商品コードが分からない商品をワサビで探す（重複チェックの前に）
    for m in ("yahoo", "auction"):
        fill_missing_skus(cfg, [o for _, mall, o in out if mall == m], MALL_JA[m], log)

    alerts, losers = find_duplicates(out, history)
    stamp = datetime.now(JST)
    held = []
    if losers and hold_dupes:
        held = [x for x in out if x[0] in losers]
        out = [x for x in out if x[0] not in losers]
    if alerts:
        cfg["output_dir"].mkdir(parents=True, exist_ok=True)
        apath = cfg["output_dir"] / f"重複SKU_アラート_{stamp:%Y%m%d%H%M%S}.csv"
        with open(apath, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["商品コード", "判定", "モール", "注文番号", "受注日", "理由"])
            w.writerows(alerts)
        log(f"！ 同じ商品の重複注文: {len({a[0] for a in alerts})}商品（一覧: {apath.name}）")
        for a in alerts[:30]:
            log(f"   {a[0]}  {a[1]}  {a[2]} {a[3]}  {a[4]}")
    if held:
        hpath = write_csv(cfg, [o for _, _, o in held], f"重複注文_要確認_{stamp:%Y%m%d%H%M%S}.csv")
        log(f"！ キャンセル候補の注文 {len(held)}件は出荷データに入れず {hpath.name} に出しました（キャンセルするか確認してください）")

    if not out:
        save_history(history)
        log("出力する注文はありません。")
        return None, alerts
    out.sort(key=lambda x: (x[2]["ordered"] or datetime.min.replace(tzinfo=JST)))
    path = write_csv(cfg, [o for _, _, o in out], f"order-{stamp:%Y%m%d%H%M%S}.csv")
    iso = stamp.isoformat(timespec="seconds")
    for key, mall, o in out:
        history["orders"][key] = iso
        for _, code, _, _ in o["items"]:
            if code:
                history["skus"][code.upper()] = {"mall": MALL_JA[mall], "order": key, "ordered": o["head"]["受注日"], "at": iso}
    save_history(history)
    by = {}
    for _, mall, _ in out:
        by[mall] = by.get(mall, 0) + 1
    log(f"作成しました: {path.name}（注文 {len(out)}件 / 行数 {sum(len(o['items']) for _, _, o in out)}）  "
        + " / ".join(f"{MALL_JA[m]} {n}件" for m, n in by.items()))
    return path, alerts


# ---------------------------------------------------------------- 画面

def main_gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext

    cfg = load_config()
    history = load_history()
    root = tk.Tk()
    root.title(APP_NAME)
    root.geometry("760x580")
    files = []  # 一覧の行ごとに、注文ファイルのパスか、取引ナビから読み取ったヤフオク1の注文（dict）

    frm = tk.Frame(root, padx=10, pady=8)
    frm.pack(fill="x")
    tk.Label(frm, text="注文日がこの日時以降の注文を対象（出荷済みリストで除外するので広めでよい）").grid(row=0, column=0, sticky="w")
    since_var = tk.StringVar(value=(datetime.now(JST) - timedelta(days=cfg["default_days"])).strftime("%Y-%m-%d 00:00"))
    tk.Entry(frm, textvariable=since_var, width=20).grid(row=0, column=1, sticky="w", padx=6)

    rak_var = tk.BooleanVar(value=bool(cfg["rakuten_secret"] and cfg["rakuten_key"]))
    tk.Checkbutton(frm, text="楽天の発送待ち注文を API で取得する", variable=rak_var).grid(row=1, column=0, columnspan=2, sticky="w")
    skip_var = tk.BooleanVar(value=True)
    tk.Checkbutton(frm, text="出力済みの注文は除く（二重出荷を防ぐ）", variable=skip_var).grid(row=2, column=0, columnspan=2, sticky="w")
    hold_var = tk.BooleanVar(value=True)
    tk.Checkbutton(frm, text="同じ商品の重複注文は、先に注文された方だけ出力する（後の方は「重複注文_要確認」に出す）",
                   variable=hold_var).grid(row=3, column=0, columnspan=2, sticky="w")

    tk.Label(root, text="ファイル（出荷済みリスト end-order.csv、Amazon の注文レポート、メルカリ・ヤフオクの注文CSV）", padx=10).pack(anchor="w")
    tk.Label(root, text="ヤフオク1（個人）: 取引ナビのページで Ctrl+A → Ctrl+C してから「取引ナビを貼り付け」（1件ずつ）",
             padx=10).pack(anchor="w")
    lst = tk.Listbox(root, height=6)
    lst.pack(fill="x", padx=10)

    def add_navi(text, source):
        o, err = parse_navi(cfg, text)
        if err:
            messagebox.showerror(APP_NAME, f"取引ナビを読み取れませんでした（{source}）。\n{err}")
            return
        aid = o["head"]["注文番号"]
        if any(isinstance(f, dict) and f["head"]["注文番号"] == aid for f in files):
            messagebox.showinfo(APP_NAME, f"オークションID {aid} はもう追加されています")
            return
        files.append(o)
        codes = " ".join(code or "（商品コードなし）" for _, code, _, _ in o["items"])
        lst.insert("end", f"[{cfg['mall']['auction']} 取引ナビ] {aid}  {codes}  {o['items'][0][0][:40]}")

    def add_files():
        for f in filedialog.askopenfilenames(title="注文ファイルを選ぶ", filetypes=[("注文ファイル", "*.csv *.txt *.tsv"), ("すべて", "*.*")]):
            if f in files:
                continue
            text = read_text(f)
            if is_navi_text(text):  # 取引ナビの文字をテキストファイルに保存したもの
                add_navi(text, Path(f).name)
                continue
            header, _ = read_table(f)
            mall = detect_mall(header)
            files.append(f)
            label = ("出荷済みリスト" if is_shipped_list(header) else "ヤフー明細" if is_yahoo_items(header)
                     else "ヤフー注文データ（使いません）" if "Order Kind" in header
                     else MALL_JA.get(mall, "不明"))
            lst.insert("end", f"[{label}] {f}")

    def paste_navi():
        try:
            text = root.clipboard_get()
        except tk.TclError:
            text = ""
        if not text.strip():
            messagebox.showerror(APP_NAME, "クリップボードが空です。取引ナビのページで Ctrl+A → Ctrl+C してから押してください")
            return
        add_navi(text, "クリップボード")

    def remove_files():
        for i in reversed(lst.curselection()):
            lst.delete(i)
            files.pop(i)

    bf = tk.Frame(root, padx=10, pady=4)
    bf.pack(fill="x")
    tk.Button(bf, text="注文ファイルを追加…", command=add_files).pack(side="left")
    tk.Button(bf, text="取引ナビを貼り付け", command=paste_navi).pack(side="left", padx=6)
    tk.Button(bf, text="選んだものを外す", command=remove_files).pack(side="left")
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
        paths = [f for f in files if isinstance(f, str)]
        navi = [f for f in files if isinstance(f, dict)]

        def work():
            try:
                path, alerts = create(cfg, since, paths, rak_var.get(), skip_var.get(), hold_var.get(), log, navi)
                if alerts:
                    skus = len({a[0] for a in alerts})
                    lines = "\n".join(f"{a[0]}  {a[1]}: {a[2]} {a[3]}" for a in alerts[:12])
                    more = "\n…（続きは出力フォルダの「重複SKU_アラート」を確認）" if len(alerts) > 12 else ""
                    root.after(0, lambda: messagebox.showwarning(
                        APP_NAME, f"同じ商品が複数の注文に入っています（{skus}商品）。\n"
                                  f"「発送」の注文を出荷し、「キャンセル候補」の注文はキャンセルを検討してください。\n\n{lines}{more}"))
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
    if not cfg["wasabi"]:
        log("ワサビのトークンが未設定です（shipping_export.ini の [WASABI]）。ヤフオクのまとめて取引の2つ目以降の商品コードは探しません")
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
