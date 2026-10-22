# -*- coding: utf-8 -*-
"""
Yahoo!ショッピングAPIの共通部品（設定・トークン・公開鍵署名・在庫API）。単体では実行しない。

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
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "yahoo_shopping_config.ini"
TOKEN_URL = "https://auth.login.yahoo.co.jp/yconnect/v2/token"
HOSTS = {"production": "https://circus.shopping.yahooapis.jp", "test": "https://test.circus.shopping.yahooapis.jp"}


# ---------------------------------------------------------------- 設定・トークン

def load_config(env=None):
    if not CONFIG_FILE.exists():
        raise SystemExit(f"設定ファイルがありません: {CONFIG_FILE}（yahoo_shopping_config.ini.sample をコピーして作成してください）")
    c = configparser.ConfigParser()
    c.read(CONFIG_FILE, encoding="utf-8")
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
    cfg["token_file"] = BASE_DIR / f"yahoo_shopping_token_{cfg['environment']}.json"
    return cfg


def save_token(cfg, tok):
    tok["obtained_at"] = int(time.time())
    cfg["token_file"].write_text(json.dumps(tok, ensure_ascii=False, indent=1), encoding="utf-8")


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
    save_token(cfg, new)
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
        path = BASE_DIR / path
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
