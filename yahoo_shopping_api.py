# -*- coding: utf-8 -*-
"""
Yahoo!ショッピングAPIの共通部品（設定・トークン・公開鍵署名・在庫API・注文API）。単体では実行しない。

設定: yahoo_shopping_config.ini
  [YAHOO_SHOPPING]       普段使う設定（environment = production / test）
  [YAHOO_SHOPPING_TEST]  テスト環境の設定を別に持つ場合（任意。load_config("test") で使う）
トークン: yahoo_shopping_token_<environment>.json（yahoo_auth.py が作る。.gitignore 済み）

公開鍵認証（注文API用）: "ストアアカウント:UNIX時刻" を公開鍵で RSA 暗号化（PKCS#1 v1.5）し、Base64 にして
X-sws-signature ヘッダーに、鍵のバージョンを X-sws-signature-version ヘッダーに付ける。
（標準ライブラリだけで実装している。exe にしても追加の部品が要らないように）
"""
import base64
import configparser
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "yahoo_shopping_config.ini"
TOKEN_URL = "https://auth.login.yahoo.co.jp/yconnect/v2/token"
JST = timezone(timedelta(hours=9))
HOSTS = {"production": "https://circus.shopping.yahooapis.jp", "test": "https://test.circus.shopping.yahooapis.jp"}


# ---------------------------------------------------------------- 設定・トークン

PUBLIC_KEY_NAME = "yahoo_public_key.pem"
PEM_BLOCK = re.compile(r"-----BEGIN PUBLIC KEY-----.*?-----END PUBLIC KEY-----", re.S)


def _config_text(config_file):
    """設定ファイルの中身を返す。public_key_file に公開鍵の中身（複数行）を直接貼った場合は、
    それを同じフォルダの yahoo_public_key.pem に書き出し、設定上はそのファイル名として扱う（設定ファイル自体は書き換えない）。"""
    text = config_file.read_text(encoding="utf-8-sig")
    m = PEM_BLOCK.search(text)
    if m:
        key_file = config_file.parent / PUBLIC_KEY_NAME
        body = re.sub(r"-----(BEGIN|END) PUBLIC KEY-----|\s", "", m.group(0))
        pem = ("-----BEGIN PUBLIC KEY-----\n" + "\n".join(body[i:i + 64] for i in range(0, len(body), 64))
               + "\n-----END PUBLIC KEY-----\n")
        if not key_file.exists() or key_file.read_text(encoding="ascii") != pem:
            key_file.write_text(pem, encoding="ascii", newline="\n")
        text = text[:m.start()] + PUBLIC_KEY_NAME + text[m.end():]
    return text


def load_config(env=None, config_file=None):
    """設定を読む。config_file を指定すると（出荷データ作成アプリなど、別のフォルダから使うとき）その設定ファイルを読み、
    トークン・公開鍵もそのフォルダのものを使う。"""
    config_file = Path(config_file) if config_file else CONFIG_FILE
    if not config_file.exists():
        raise SystemExit(f"設定ファイルがありません: {config_file}（yahoo_shopping_config.ini.sample をコピーして作成してください）")
    c = configparser.ConfigParser()
    c.read_string(_config_text(config_file))
    section = "YAHOO_SHOPPING_TEST" if env == "test" and c.has_section("YAHOO_SHOPPING_TEST") else "YAHOO_SHOPPING"
    s = c[section]
    cfg = {k: s.get(k, "").strip() for k in ("client_id", "client_secret", "redirect_uri", "seller_id", "environment",
                                               "public_key_file", "public_key_version")}
    for k in ("client_id", "client_secret", "redirect_uri"):
        if not cfg[k] or "ここに" in cfg[k]:
            raise SystemExit(f"yahoo_shopping_config.ini の [{section}] {k} が未入力です")
    cfg["environment"] = (cfg["environment"] or "production").lower()
    if cfg["environment"] not in HOSTS:
        raise SystemExit("yahoo_shopping_config.ini の environment は production か test にしてください")
    cfg["host"] = HOSTS[cfg["environment"]]
    cfg["base_dir"] = config_file.parent
    cfg["token_file"] = cfg["base_dir"] / f"yahoo_shopping_token_{cfg['environment']}.json"
    return cfg


REAUTH_SECONDS = 28 * 86400  # 公開鍵認証ありのリフレッシュトークンの有効期限（ログインから4週間）
REAUTH_SECONDS_NO_KEY = 12 * 3600  # 公開鍵認証なしの場合（12時間）


def save_token(cfg, tok, authorized_at=None):
    """トークンを保存する。authorized_at は yahoo_auth.bat でログインした日時（UNIX時刻）で、更新しても引き継ぐ。"""
    tok["obtained_at"] = int(time.time())
    if authorized_at:
        tok["authorized_at"] = int(authorized_at)
    cfg["token_file"].write_text(json.dumps(tok, ensure_ascii=False, indent=1), encoding="utf-8")


def reauth_deadline(cfg):
    """yahoo_auth.bat でログインし直す期限（UNIX時刻）。ログイン日時が記録されていなければ None。"""
    if not cfg["token_file"].exists():
        return None
    at = json.loads(cfg["token_file"].read_text(encoding="utf-8")).get("authorized_at")
    if not at:
        return None
    return at + (REAUTH_SECONDS if cfg["public_key_file"] and cfg["public_key_version"] else REAUTH_SECONDS_NO_KEY)


def access_token(cfg):
    """保存済みのアクセストークンを返す。期限切れ間近ならリフレッシュトークンで更新する。"""
    if not cfg["token_file"].exists():
        raise SystemExit(f"トークンがありません（{cfg['environment']}）。yahoo_auth.bat でログインしてください")
    tok = json.loads(cfg["token_file"].read_text(encoding="utf-8"))
    if time.time() - tok.get("obtained_at", 0) < int(tok.get("expires_in", 3600)) - 120:
        return tok["access_token"]
    r = requests.post(TOKEN_URL, timeout=60, auth=(cfg["client_id"], cfg["client_secret"]),
                      data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
    if r.status_code != 200:
        raise SystemExit(f"トークンの更新に失敗しました（HTTP {r.status_code}）。yahoo_auth.bat でログインし直してください")
    new = r.json()
    new.setdefault("refresh_token", tok["refresh_token"])
    save_token(cfg, new, tok.get("authorized_at"))
    return new["access_token"]


# ---------------------------------------------------------------- 公開鍵署名（RSA PKCS#1 v1.5、標準ライブラリのみ）

def _der_read(data, pos):
    """DER の1要素を読む。戻り値は (タグ, 中身, 次の位置)。"""
    tag = data[pos]
    length = data[pos + 1]
    pos += 2
    if length & 0x80:
        n = length & 0x7F
        length = int.from_bytes(data[pos:pos + n], "big")
        pos += n
    return tag, data[pos:pos + length], pos + length


def load_public_key(path):
    """PEM（-----BEGIN PUBLIC KEY-----）から RSA の (n, e) を取り出す。"""
    text = Path(path).read_text(encoding="ascii")
    body = "".join(l for l in text.splitlines() if l and not l.startswith("-----"))
    der = base64.b64decode(body)
    _, spki, _ = _der_read(der, 0)                 # SubjectPublicKeyInfo
    _, _alg, pos = _der_read(spki, 0)              # AlgorithmIdentifier
    tag, bitstr, _ = _der_read(spki, pos)          # BIT STRING
    if tag != 0x03:
        raise ValueError("公開鍵の形式が想定と違います")
    _, rsakey, _ = _der_read(bitstr[1:], 0)        # 先頭1バイトは未使用ビット数
    _, n_bytes, pos = _der_read(rsakey, 0)
    _, e_bytes, _ = _der_read(rsakey, pos)
    return int.from_bytes(n_bytes, "big"), int.from_bytes(e_bytes, "big")


def rsa_pkcs1_encrypt(message, n, e):
    k = (n.bit_length() + 7) // 8
    if len(message) > k - 11:
        raise ValueError("署名する文字列が長すぎます")
    ps = b""
    while len(ps) < k - len(message) - 3:
        ps += bytes(b for b in os.urandom(k) if b != 0)
    em = b"\x00\x02" + ps[:k - len(message) - 3] + b"\x00" + message
    return pow(int.from_bytes(em, "big"), e, n).to_bytes(k, "big")


def signature_headers(cfg):
    """公開鍵認証のヘッダー。公開鍵が設定されていなければ空。"""
    if not cfg["public_key_file"] or not cfg["public_key_version"]:
        return {}
    path = Path(cfg["public_key_file"])
    if not path.is_absolute():
        path = cfg["base_dir"] / path
    n, e = load_public_key(path)
    plain = f"{cfg['seller_id']}:{int(time.time())}".encode("ascii")
    return {"X-sws-signature": base64.b64encode(rsa_pkcs1_encrypt(plain, n, e)).decode("ascii"),
            "X-sws-signature-version": cfg["public_key_version"]}


# ---------------------------------------------------------------- API 呼び出し

def call(cfg, api, data=None, xml=None):
    """API を POST で呼ぶ。data はフォーム（在庫API など）、xml は XML 本文（注文API）。レスポンスを返す。"""
    headers = {"Authorization": f"Bearer {access_token(cfg)}"}
    headers.update(signature_headers(cfg))
    if xml is not None:
        headers["Content-Type"] = "application/xml; charset=utf-8"
        body = xml.encode("utf-8")
    else:
        body = data
    return requests.post(f"{cfg['host']}/ShoppingWebService/V1/{api}", headers=headers, data=body, timeout=60)


def error_of(resp):
    """エラー応答から (コード, メッセージ) を取り出す。"""
    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError:
        return "", resp.text[:200]
    err = root if root.tag == "Error" else root.find(".//Error")
    if err is None:
        return "", ""
    return err.findtext("Code") or "", err.findtext("Message") or ""


class ApiError(RuntimeError):
    """API のエラー応答。status は HTTP ステータス、code は px-04306 などのエラーコード。"""

    def __init__(self, api, resp):
        self.status = resp.status_code
        self.code, msg = error_of(resp)
        super().__init__(f"{api} HTTP {self.status} {self.code}: {msg}")


# ---------------------------------------------------------------- 注文API（利用申請の承認が必要）

def order_list(cfg, start, end):
    """注文検索API。注文日時が start〜end の注文を [(注文ID, 注文日時, 注文ステータス)] で返す（古い順）。
    注文IDは「ストアアカウント-番号」（例 vivoage-10216169）。注文ステータス 4 はキャンセル。"""
    out, pos = [], 1
    while True:
        xml = (f"<Req><Search><Result>500</Result><Start>{pos}</Start><Sort>+order_time</Sort>"
               f"<Condition><OrderTimeFrom>{start:%Y%m%d%H%M%S}</OrderTimeFrom><OrderTimeTo>{end:%Y%m%d%H%M%S}</OrderTimeTo>"
               f"</Condition><Field>OrderId,OrderTime,OrderStatus</Field></Search>"
               f"<SellerId>{cfg['seller_id']}</SellerId></Req>")
        r = call(cfg, "orderList", xml=xml)
        if r.status_code != 200:
            raise ApiError("orderList", r)
        root = ET.fromstring(r.content)
        infos = list(root.iter("OrderInfo"))
        out += [(o.findtext("OrderId") or "", o.findtext("OrderTime") or "", o.findtext("OrderStatus") or "") for o in infos]
        pos += len(infos)
        if not infos or pos > int(root.findtext(".//TotalCount") or 0):
            return out
        time.sleep(1)  # 1クエリー/秒


def order_items(cfg, order_id):
    """注文詳細API。(注文ステータス, [(商品コード, サブコード, 数量, 商品名)]) を返す。"""
    xml = (f"<Req><Target><OrderId>{order_id}</OrderId><Field>OrderId,OrderStatus,LineId,ItemId,SubCode,Quantity,Title</Field>"
           f"</Target><SellerId>{cfg['seller_id']}</SellerId></Req>")
    r = call(cfg, "orderInfo", xml=xml)
    if r.status_code != 200:
        raise ApiError("orderInfo", r)
    info = ET.fromstring(r.content).find(".//OrderInfo")
    if info is None:
        raise RuntimeError(f"orderInfo: {order_id} の注文情報がありません")
    return info.findtext("OrderStatus") or "", [
        ((it.findtext("ItemId") or "").strip(), (it.findtext("SubCode") or "").strip(), (it.findtext("Quantity") or "").strip(),
         (it.findtext("Title") or "").strip())
        for it in info.iter("Item")]


def _fields(el):
    """XML の要素の子を {項目名: 値} にする。商品（Item）は "Item" に [{項目名: 値}] で入れる。"""
    row = {"Item": []}
    for ch in el:
        if ch.tag == "Item":
            row["Item"].append({c.tag: (c.text or "").strip() for c in ch})
        else:
            row[ch.tag] = (ch.text or "").strip()
    return row


def shipped_orders(cfg, first, last, fields, lookback_days=120):
    """出荷日が first〜last（date）の注文を [{項目名: 値, "Item": [商品]}] で返す（注文検索API）。
    注文日時の指定が必須のため、first の lookback_days 日前から last までに注文されたものを探す。
    商品は1行目しか返らない（2行目以降は order_lines で取る）。"""
    now = datetime.now(JST)
    start = datetime(first.year, first.month, first.day, tzinfo=JST) - timedelta(days=lookback_days)
    end = min(datetime(last.year, last.month, last.day, 23, 59, 59, tzinfo=JST), now)
    out, pos = [], 1
    while True:
        xml = (f"<Req><Search><Result>2000</Result><Start>{pos}</Start><Sort>+order_time</Sort>"
               f"<Condition><OrderTimeFrom>{start:%Y%m%d%H%M%S}</OrderTimeFrom><OrderTimeTo>{end:%Y%m%d%H%M%S}</OrderTimeTo>"
               f"<ShipDateFrom>{first:%Y%m%d}</ShipDateFrom><ShipDateTo>{last:%Y%m%d}</ShipDateTo></Condition>"
               f"<Field>{fields}</Field></Search><SellerId>{cfg['seller_id']}</SellerId></Req>")
        r = call(cfg, "orderList", xml=xml)
        if r.status_code != 200:
            raise ApiError("orderList", r)
        root = ET.fromstring(r.content)
        infos = list(root.iter("OrderInfo"))
        out += [_fields(o) for o in infos]
        pos += len(infos)
        if not infos or pos > int(root.findtext(".//TotalCount") or 0):
            return out
        time.sleep(1)  # 1クエリー/秒


def order_lines(cfg, order_id):
    """注文詳細APIで、注文のすべての商品を [{LineId, ItemId, SubCode, Title, Quantity, UnitPrice}] で返す。"""
    xml = (f"<Req><Target><OrderId>{order_id}</OrderId><Field>OrderId,LineId,ItemId,SubCode,Title,Quantity,UnitPrice</Field>"
           f"</Target><SellerId>{cfg['seller_id']}</SellerId></Req>")
    r = call(cfg, "orderInfo", xml=xml)
    if r.status_code != 200:
        raise ApiError("orderInfo", r)
    info = ET.fromstring(r.content).find(".//OrderInfo")
    if info is None:
        raise RuntimeError(f"orderInfo: {order_id} の注文情報がありません")
    return _fields(info)["Item"]


def get_stock(cfg, item_codes):
    """在庫参照API。{商品コード: {"quantity", "status", "published", "updated"}} を返す（1回 1,000件まで）。"""
    result = {}
    for i in range(0, len(item_codes), 1000):
        r = call(cfg, "getStock", data={"seller_id": cfg["seller_id"], "item_code": ",".join(item_codes[i:i + 1000])})
        if r.status_code != 200:
            code, msg = error_of(r)
            raise RuntimeError(f"在庫参照APIエラー HTTP {r.status_code} {code}: {msg}")
        root = ET.fromstring(r.content)
        for res in root.iter("Result"):
            code = res.findtext("ItemCode") or ""
            sub = res.findtext("SubCode") or ""
            result[f"{code}:{sub}" if sub else code] = {
                "quantity": res.findtext("Quantity"), "status": res.findtext("Status"),
                "published": res.findtext("IsPublished"), "updated": res.findtext("UpdateTime")}
        if i + 1000 < len(item_codes):
            time.sleep(1)  # 1クエリー/秒
    return result


def set_stock_zero(cfg, item_codes):
    """在庫更新API で在庫を0にする。戻り値は {商品コード: None（成功） or エラーコード}。
    1件でもエラーがあるとまとめて取り消されるため、まとめて失敗したら1件ずつ送り直す。"""
    out = {}

    def send(codes):
        r = call(cfg, "setStock", data={"seller_id": cfg["seller_id"], "item_code": ",".join(codes),
                                       "quantity": ",".join("0" for _ in codes)})
        if r.status_code not in (200, 207):
            code, msg = error_of(r)
            return {c: f"HTTP{r.status_code} {code}" for c in codes}
        res = {}
        root = ET.fromstring(r.content)
        for x in root.iter("Result"):
            ic, sub = x.findtext("ItemCode") or "", x.findtext("SubCode") or ""
            key = f"{ic}:{sub}" if sub else ic
            err = x.findtext("ErrorCode")
            res[key] = err if err else (None if (x.findtext("Quantity") or "") == "0" else f"Quantity={x.findtext('Quantity')}")
        for c in codes:  # 応答に無いものは失敗扱い
            res.setdefault(c, "応答なし")
        return res

    for i in range(0, len(item_codes), 100):
        batch = item_codes[i:i + 100]
        res = send(batch)
        if any(v for v in res.values()) and len(batch) > 1:
            res = {}
            for c in batch:
                time.sleep(1)  # 1クエリー/秒
                res.update(send([c]))
        out.update(res)
        time.sleep(1)
    return out
