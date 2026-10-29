# -*- coding: utf-8 -*-
"""
ワサビスイッチ（WASABI SWITCH）の API の共通部品（トークン・呼び出し・商品検索）。単体では実行しない。

トークン: wasabi_config.ini の [WASABI] token（KEY:SECRET）と base_url。
          設定ファイルに無ければ環境変数 WASABI_AUTH（GitHub Actions の売上集計）。トークンはログに出さない。
/api/shops は呼ばない（接続モールの認証情報が返るため）。
"""
import configparser
import logging
import os
import time
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "wasabi_config.ini"
BASE_URL = "https://world-switch.com/kaitoriouji/ext"

log = logging.getLogger("wasabi_api")


def load_config(config_file=None):
    """{"base_url", "token"} を返す。トークンがどこにも無ければ None。"""
    c = configparser.ConfigParser()
    c.read(Path(config_file) if config_file else CONFIG_FILE, encoding="utf-8")
    w = c["WASABI"] if c.has_section("WASABI") else {}
    token = w.get("token", "").strip()
    if not token or "ここに" in token:
        token = os.environ.get("WASABI_AUTH", "").strip()
    if not token:
        return None
    return {"base_url": (w.get("base_url", "") or BASE_URL).strip().rstrip("/"), "token": token}


def request(cfg, method, path, retries=4, timeout=60, **kwargs):
    """API を呼んで JSON を返す。通信エラー・429・500番台は少し待ってやり直す（3秒、6秒、9秒…）。
    cfg は {"base_url", "token"} を含む辞書（load_config の戻り値か、それと同じキーを持つ各スクリプトの設定）。"""
    url = f"{cfg['base_url'].rstrip('/')}{path}"
    headers = {"Authorization": f"Bearer {cfg['token']}", "Content-Type": "application/json", "Accept": "application/json"}
    for attempt in range(1, retries + 1):
        try:
            r = requests.request(method, url, headers=headers, timeout=timeout, **kwargs)
        except requests.RequestException as e:
            if attempt == retries:
                raise RuntimeError(f"ワサビ {path}: 通信エラー（{type(e).__name__}）") from e
            log.warning(f"ワサビ {path}: 通信エラー（{type(e).__name__}）。やり直します {attempt}/{retries - 1}")
            time.sleep(3 * attempt)
            continue
        if (r.status_code == 429 or r.status_code >= 500) and attempt < retries:
            log.warning(f"ワサビ {path}: HTTP {r.status_code}。やり直します {attempt}/{retries - 1}")
            time.sleep(3 * attempt)
            continue
        if r.status_code != 200:
            raise RuntimeError(f"ワサビ {path}: HTTP {r.status_code}: {r.text[:300]}")
        return r.json()


def search_products(cfg, query, page=1, limit=100, sort="product_id", **kwargs):
    """/api/products/search を1ページ取得する。戻り値は {"data": [商品], "total": 該当件数, ...}。"""
    body = {"sort": sort, "reverse": False, "page": page, "limit": limit, "query": query or {}}
    return request(cfg, "POST", "/api/products/search", json=body, **kwargs)
