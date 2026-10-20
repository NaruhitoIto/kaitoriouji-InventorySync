# -*- coding: utf-8 -*-
"""Amazon SP-API の共通部品（認証・リクエスト・設定読み込み）。単体では実行しない。"""
import configparser
import logging
import time
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
AMAZON_CFG = BASE_DIR / "amazon_sp_api_config.ini"
LWA_URL = "https://api.amazon.com/auth/o2/token"

log = logging.getLogger("amazon_sp_api")

_token_cache = {"token": None, "expires": 0.0}


def load_amazon_config():
    if not AMAZON_CFG.exists():
        raise SystemExit(f"設定ファイルがありません: {AMAZON_CFG}（amazon_sp_api_config.ini.sample をコピーして作成してください）")
    c = configparser.ConfigParser()
    c.read(AMAZON_CFG, encoding="utf-8")
    sec = c["AMAZON_SP_API"]
    for k in ("client_id", "client_secret", "refresh_token", "seller_id"):
        v = sec.get(k, "").strip()
        if not v or "ここに" in v:
            raise SystemExit(f"amazon_sp_api_config.ini の {k} が未入力です")
    return {
        "client_id": sec["client_id"].strip(),
        "client_secret": sec["client_secret"].strip(),
        "refresh_token": sec["refresh_token"].strip(),
        "seller_id": sec["seller_id"].strip(),
        "marketplace_id": sec.get("marketplace_id", "A1VC38T7YXB528").strip(),
        "endpoint": sec.get("endpoint", "https://sellingpartnerapi-fe.amazon.com").strip().rstrip("/"),
        "orders_last_checked": sec.get("orders_last_checked", "").strip(),
    }


def get_access_token(acfg):
    """LWA アクセストークン（約1時間有効）を取得。期限前はキャッシュを使う。"""
    if _token_cache["token"] and time.time() < _token_cache["expires"] - 60:
        return _token_cache["token"]
    r = requests.post(
        LWA_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": acfg["refresh_token"],
            "client_id": acfg["client_id"],
            "client_secret": acfg["client_secret"],
        },
        timeout=60,
    )
    if r.status_code != 200:
        raise RuntimeError(f"LWA認証に失敗しました HTTP {r.status_code}: {r.text[:300]}")
    j = r.json()
    _token_cache["token"] = j["access_token"]
    _token_cache["expires"] = time.time() + int(j.get("expires_in", 3600))
    return _token_cache["token"]


def sp_request(acfg, method, path, params=None, json_body=None, retries=5):
    """SP-API を呼ぶ。429/5xx は待ってリトライ。成功時は JSON を返す。"""
    url = f"{acfg['endpoint']}{path}"
    for attempt in range(retries):
        headers = {
            "x-amz-access-token": get_access_token(acfg),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            r = requests.request(method, url, headers=headers, params=params, json=json_body, timeout=60)
        except requests.RequestException as e:
            log.warning(f"通信エラー {path}（試行 {attempt + 1}）: {type(e).__name__}")
            time.sleep(3 * (attempt + 1))
            continue
        if r.status_code in (429, 500, 502, 503, 504):
            wait = float(r.headers.get("x-amzn-RateLimit-Limit", 0) or 0)
            time.sleep(max(2 * (attempt + 1), (1 / wait) if wait else 0))
            continue
        if r.status_code == 401:
            _token_cache["token"] = None  # 期限切れの可能性。取り直して再試行
            time.sleep(1)
            continue
        if r.status_code not in (200, 201, 202):
            raise RuntimeError(f"SP-API HTTP {r.status_code} {path}: {r.text[:500]}")
        return r.json() if r.content else {}
    raise RuntimeError(f"SP-API リトライ上限に達しました: {path}")
