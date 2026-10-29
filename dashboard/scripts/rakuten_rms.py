"""楽天RMS 受注API(RakutenPay受注API)から、1日分の受注・発送を集計する。

- 認証: 環境変数 RAKUTEN_SERVICE_SECRET / RAKUTEN_LICENSE_KEY (GitHub Secrets)
- 受注: 注文日(dateType=1)で検索、キャンセル(800/900)を除外
- 発送: 発送日(dateType=4)で検索、キャンセルを除外
- 注文の中身(氏名・住所など)はメモリ上で合計を出すのに使うだけで、
  ログやファイルには一切出力しない。
"""
import base64
import os
import time

import requests

BASE = "https://api.rms.rakuten.co.jp/es/2.0/order"
ALL_PROGRESS = [100, 200, 300, 400, 500, 600, 700, 800, 900]
CANCEL_PROGRESS = {800, 900}  # キャンセル確定待ち / キャンセル確定
DATE_TYPE_ORDER = 1  # 注文日
DATE_TYPE_SHIP = 4   # 発送日
AMOUNT_FIELDS = ("totalPrice", "requestPrice", "goodsPrice")
AMOUNT_FIELD = os.environ.get("RAKUTEN_AMOUNT_FIELD") or "totalPrice"
GET_ORDER_VERSION = 7
WAIT_SEC = 1.0
RETRIES = 5


def enabled():
    return bool(os.environ.get("RAKUTEN_SERVICE_SECRET") and os.environ.get("RAKUTEN_LICENSE_KEY"))


def _headers():
    raw = f"{os.environ['RAKUTEN_SERVICE_SECRET']}:{os.environ['RAKUTEN_LICENSE_KEY']}"
    token = base64.b64encode(raw.encode("utf-8")).decode("ascii")
    return {"Authorization": f"ESA {token}", "Content-Type": "application/json; charset=utf-8"}


def _post(path, body):
    for attempt in range(1, RETRIES + 1):
        try:
            r = requests.post(f"{BASE}/{path}/", headers=_headers(), json=body, timeout=60)
        except (requests.Timeout, requests.ConnectionError) as e:
            if attempt == RETRIES:
                raise
            print(f"rakuten {path}: retry {attempt} after error {type(e).__name__}")
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 429 or r.status_code >= 500:
            if attempt == RETRIES:
                raise RuntimeError(f"rakuten {path}: HTTP {r.status_code}")
            print(f"rakuten {path}: retry {attempt} after HTTP {r.status_code}")
            time.sleep(2 ** attempt)
            continue
        if r.status_code != 200:
            # エラー応答はメッセージコードのみ。注文内容は含まれない。
            raise RuntimeError(f"rakuten {path}: HTTP {r.status_code} {r.text[:300]}")
        time.sleep(WAIT_SEC)
        return r.json()


def _search(d, date_type):
    numbers, page = [], 1
    while True:
        res = _post("searchOrder", {
            "dateType": date_type,
            "startDatetime": f"{d}T00:00:00+0900",
            "endDatetime": f"{d}T23:59:59+0900",
            "orderProgressList": ALL_PROGRESS,
            "PaginationRequestModel": {"requestRecordsAmount": 1000, "requestPage": page},
        })
        numbers += res.get("orderNumberList") or []
        total_pages = (res.get("PaginationResponseModel") or {}).get("totalPages") or 1
        if page >= total_pages:
            return numbers
        page += 1


def _get_orders(numbers):
    orders = []
    for i in range(0, len(numbers), 100):
        res = _post("getOrder", {"orderNumberList": numbers[i:i + 100], "version": GET_ORDER_VERSION})
        orders += res.get("OrderModelList") or []
    return orders


def _summarize(orders):
    out = {"count": 0, "qty": 0, **{f: 0 for f in AMOUNT_FIELDS}}
    for o in orders:
        if int(o.get("orderProgress") or 0) in CANCEL_PROGRESS:
            continue
        out["count"] += 1
        for f in AMOUNT_FIELDS:
            out[f] += int(o.get(f) or 0)
        for pkg in o.get("PackageModelList") or []:
            for item in pkg.get("ItemModelList") or []:
                if int(item.get("deleteItemFlag") or 0) == 1:
                    continue
                out["qty"] += int(item.get("units") or 0)
    return out


def fetch_day(d):
    """d: 'YYYY-MM-DD' (JST)。受注・発送の集計を返す。"""
    ordered = _summarize(_get_orders(_search(d, DATE_TYPE_ORDER)))
    shipped = _summarize(_get_orders(_search(d, DATE_TYPE_SHIP)))
    return {
        "ordered": ordered[AMOUNT_FIELD],
        "qty": ordered["qty"],
        "count": ordered["count"],
        "shipped": shipped[AMOUNT_FIELD],
        "shipped_count": shipped["count"],
        "candidates": {f: ordered[f] for f in AMOUNT_FIELDS},
    }
