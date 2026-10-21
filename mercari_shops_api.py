# -*- coding: utf-8 -*-
"""メルカリShops API（GraphQL）の共通部品（設定読み込み・リクエスト・レート制限）。単体では実行しない。

仕様: https://api.mercari-shops.com/docs/index.html
- 認証は管理画面で発行する Personal API Access Token（Bearer）。User-Agent は「事業者識別名/バージョン」が必須。
- 登録した固定IP以外からは 404 になる（本番・Sandbox で別々に登録）。
- レート制限はショップ単位で 10,000 ポイント/時。超過すると HTTP 400 + TOO_MANY_REQUESTS。
"""
import configparser
import logging
import time
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
MERCARI_CFG = BASE_DIR / "mercari_shops_config.ini"
ENDPOINTS = {
    "production": "https://api.mercari-shops.com/v1/graphql",
    "sandbox": "https://api.mercari-shops-sandbox.com/v1/graphql",
}
RETRY_CODES = {"INTERNAL_SERVER_ERROR", "DEADLINE_EXCEEDED"}  # 一時的なエラー（リトライで解消することが多い）
RATE_WAIT_MAX = 120  # レート制限のリセットまでこの秒数以内なら待って再試行する。超えるなら RateLimited を投げる

log = logging.getLogger("mercari_shops_api")

# 直近のレスポンスヘッダーから読んだレート制限の状況（呼び出し側が残りポイントを見て処理を止めるのに使う）
rate = {"limit": None, "remaining": None, "reset": None, "used_total": 0}


class RateLimited(Exception):
    """レート制限の上限に達し、すぐには回復しない。reset はリセットまでの秒数。"""

    def __init__(self, reset):
        super().__init__(f"メルカリShops API のレート制限に達しました（リセットまで約{reset}秒）")
        self.reset = reset


class GraphQLError(Exception):
    """HTTP 200 で errors が返った（入力不正・存在しない商品など）。codes はエラーコードの一覧。"""

    def __init__(self, errors):
        self.errors = errors
        self.codes = [error_code(e) for e in errors]
        super().__init__("; ".join(f"{error_code(e)}: {e.get('message')}" for e in errors)[:800])


def error_code(err):
    ext = err.get("extensions") or {}
    return ext.get("errorCode") or ext.get("code") or "UNKNOWN"


def load_mercari_config():
    if not MERCARI_CFG.exists():
        raise SystemExit(f"設定ファイルがありません: {MERCARI_CFG}（mercari_shops_config.ini.sample をコピーして作成してください）")
    c = configparser.ConfigParser()
    c.read(MERCARI_CFG, encoding="utf-8")
    sec = c["MERCARI_SHOPS"]
    for k in ("token", "user_agent"):
        v = sec.get(k, "").strip()
        if not v or "ここに" in v:
            raise SystemExit(f"mercari_shops_config.ini の {k} が未入力です")
    env = sec.get("environment", "sandbox").strip().lower()
    if env not in ENDPOINTS:
        raise SystemExit(f"mercari_shops_config.ini の environment は production か sandbox にしてください（現在: {env}）")
    return {
        "token": sec["token"].strip(),
        "user_agent": sec["user_agent"].strip(),
        "environment": env,
        "endpoint": ENDPOINTS[env],
        "orders_last_checked": sec.get("orders_last_checked", "").strip(),
        "rate_reserve": sec.getint("rate_reserve", fallback=1500),
    }


def _update_rate(headers):
    def num(name):
        try:
            return int(float(headers.get(name)))
        except (TypeError, ValueError):
            return None

    rate["limit"] = num("X-Ratelimit-Limit")
    rate["remaining"] = num("X-Ratelimit-Remaining")
    rate["reset"] = num("X-Ratelimit-Reset")
    used = num("X-RateLimit-Used")
    if used:
        rate["used_total"] += used


def _rate_limit_error(body):
    """400 の本文が TOO_MANY_REQUESTS かどうか（仕様書では {"error": {"errors": [...]}} の形）。"""
    if not isinstance(body, dict):
        return False
    errors = body.get("errors") or (body.get("error") or {}).get("errors") or []
    return any(error_code(e) == "TOO_MANY_REQUESTS" for e in errors)


def gql(mcfg, query, variables=None, retries=5):
    """GraphQL を1回呼んで data を返す。通信エラー・5xx・一時エラーはリトライする。"""
    headers = {
        "Authorization": f"Bearer {mcfg['token']}",
        "User-Agent": mcfg["user_agent"],
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    body = {"query": query, "variables": variables or {}}
    for attempt in range(retries):
        try:
            r = requests.post(mcfg["endpoint"], headers=headers, json=body, timeout=60)
        except requests.RequestException as e:
            log.warning(f"通信エラー（試行 {attempt + 1}）: {type(e).__name__}")
            time.sleep(3 * (attempt + 1))
            continue
        _update_rate(r.headers)
        try:
            j = r.json()
        except ValueError:
            j = None
        if r.status_code == 400 and _rate_limit_error(j):
            reset = rate["reset"] if rate["reset"] is not None else 60
            if reset > RATE_WAIT_MAX:
                raise RateLimited(reset)
            log.warning(f"レート制限に達したため {reset}秒待って再試行します")
            time.sleep(reset + 1)
            continue
        if r.status_code in (500, 502, 503, 504):
            time.sleep(3 * (attempt + 1))
            continue
        if r.status_code == 401:
            raise RuntimeError("メルカリShops API 認証エラー（HTTP 401）。トークンと environment（本番/Sandbox）の組み合わせを確認してください")
        if r.status_code == 404:
            raise RuntimeError("メルカリShops API に接続できません（HTTP 404）。このPCの固定IPが、この環境（本番/Sandbox）で登録済みか確認してください")
        if r.status_code != 200 or j is None:
            raise RuntimeError(f"メルカリShops API HTTP {r.status_code}: {r.text[:500]}")
        errors = j.get("errors")
        if errors:
            if all(error_code(e) in RETRY_CODES for e in errors) and attempt < retries - 1:
                log.warning(f"一時的なエラーのため再試行します（試行 {attempt + 1}）: {[error_code(e) for e in errors]}")
                time.sleep(3 * (attempt + 1))
                continue
            raise GraphQLError(errors)
        return j.get("data") or {}
    raise RuntimeError("メルカリShops API リトライ上限に達しました")


def rate_low(mcfg):
    """残りポイントが予備分（rate_reserve）を下回ったら True。ヘッダーが無いうちは False。"""
    return rate["remaining"] is not None and rate["remaining"] < mcfg["rate_reserve"]


def rate_summary():
    return f"残り {rate['remaining']}/{rate['limit']} ポイント（リセットまで {rate['reset']}秒、今回の消費 {rate['used_total']}）"
