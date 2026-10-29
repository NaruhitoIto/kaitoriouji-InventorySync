# -*- coding: utf-8 -*-
"""
楽天RMS 受注API（RakutenPay受注API）の共通部品（キー・注文検索・注文詳細）。単体では実行しない。

キー: rakuten_api_config.ini の [RAKUTEN_API] service_secret / license_key。
      設定ファイルに無ければ環境変数 RAKUTEN_SERVICE_SECRET / RAKUTEN_LICENSE_KEY（GitHub Actions の売上集計）。
注文の中身（氏名・住所など）はログに出さない。エラー応答はメッセージコードだけで、注文内容は含まれない。
"""
import base64
import configparser
import logging
import os
import time
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "rakuten_api_config.ini"
API = "https://api.rms.rakuten.co.jp/es/2.0/order"
DATE_ORDER = 1  # dateType: 注文日
DATE_SHIP = 4   # dateType: 発送日
DT_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
WAIT_SEC = 1.0  # 呼び出しの間隔（これより短い間隔で続けて呼ばない）
RETRIES = 5

log = logging.getLogger("rakuten_rms_api")
_last_call = 0.0


def load_keys(config_file=None):
    """(service_secret, license_key) を返す。どこにも無ければ None。"""
    c = configparser.ConfigParser()
    c.read(Path(config_file) if config_file else CONFIG_FILE, encoding="utf-8")
    if c.has_section("RAKUTEN_API"):
        secret = c["RAKUTEN_API"].get("service_secret", "").strip()
        key = c["RAKUTEN_API"].get("license_key", "").strip()
        if secret and key:
            return secret, key
    secret = os.environ.get("RAKUTEN_SERVICE_SECRET", "").strip()
    key = os.environ.get("RAKUTEN_LICENSE_KEY", "").strip()
    return (secret, key) if secret and key else None


def headers(keys):
    token = base64.b64encode(f"{keys[0]}:{keys[1]}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"ESA {token}", "Content-Type": "application/json; charset=UTF-8"}


def post(keys, api, body):
    """受注API（searchOrder / getOrder など）を呼んで JSON を返す。通信エラー・429・500番台は待ってやり直す。"""
    global _last_call
    for attempt in range(1, RETRIES + 1):
        time.sleep(max(0.0, _last_call + WAIT_SEC - time.time()))
        _last_call = time.time()
        try:
            r = requests.post(f"{API}/{api}/", headers=headers(keys), json=body, timeout=60)
        except (requests.Timeout, requests.ConnectionError) as e:
            if attempt == RETRIES:
                raise
            log.warning(f"楽天 {api}: 通信エラー（{type(e).__name__}）。やり直します {attempt}/{RETRIES - 1}")
            time.sleep(2 ** attempt)
            continue
        if (r.status_code == 429 or r.status_code >= 500) and attempt < RETRIES:
            log.warning(f"楽天 {api}: HTTP {r.status_code}。やり直します {attempt}/{RETRIES - 1}")
            time.sleep(2 ** attempt)
            continue
        if r.status_code != 200:
            raise RuntimeError(f"楽天 {api} エラー HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        errors = [m for m in data.get("MessageModelList") or [] if m.get("messageType") == "ERROR"]
        if errors:
            raise RuntimeError(f"楽天 {api} エラー: {errors}")
        return data


def search(keys, start, end, date_type=DATE_ORDER, progress=None):
    """注文検索API。start〜end（タイムゾーン付きの datetime）の注文番号を全ページ分返す。
    date_type は DATE_ORDER（注文日）か DATE_SHIP（発送日）。progress は進捗の絞り込み（[300] など。None なら全部）。"""
    numbers, page = [], 1
    while True:
        body = {"dateType": date_type, "startDatetime": start.strftime(DT_FORMAT), "endDatetime": end.strftime(DT_FORMAT),
                "PaginationRequestModel": {"requestRecordsAmount": 1000, "requestPage": page}}
        if progress:
            body["orderProgressList"] = list(progress)
        data = post(keys, "searchOrder", body)
        got = data.get("orderNumberList") or []
        numbers += got
        if not got or page >= int((data.get("PaginationResponseModel") or {}).get("totalPages") or 1):
            return numbers
        page += 1


def get_orders(keys, numbers, version=7, progress=None):
    """注文詳細API。注文番号のリストの注文（OrderModel）を100件ずつ取って返す。progress(done, total) は進み具合の通知用。"""
    orders = []
    for i in range(0, len(numbers), 100):
        orders += post(keys, "getOrder", {"orderNumberList": numbers[i:i + 100], "version": version}).get("OrderModelList") or []
        if progress:
            progress(min(i + 100, len(numbers)), len(numbers))
    return orders
