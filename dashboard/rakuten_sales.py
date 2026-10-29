"""楽天RMS 受注API(RakutenPay受注API)から、1日分の受注・発送を集計する。API の呼び出しは共通部品 rakuten_rms_api。

- 認証: Actions では環境変数 RAKUTEN_SERVICE_SECRET / RAKUTEN_LICENSE_KEY (GitHub Secrets)。
  PC で試すときはリポジトリ直下の rakuten_api_config.ini
- 受注: 注文日(dateType=1)で検索、キャンセル(800/900)を除外
- 発送: 発送日(dateType=4)で検索、キャンセルを除外
- 注文の中身(氏名・住所など)はメモリ上で合計を出すのに使うだけで、
  ログやファイルには一切出力しない。
"""
import os
from datetime import datetime, timedelta, timezone

import rakuten_rms_api as rms

ALL_PROGRESS = [100, 200, 300, 400, 500, 600, 700, 800, 900]
CANCEL_PROGRESS = {800, 900}  # キャンセル確定待ち / キャンセル確定
AMOUNT_FIELDS = ("totalPrice", "requestPrice", "goodsPrice")
AMOUNT_FIELD = os.environ.get("RAKUTEN_AMOUNT_FIELD") or "totalPrice"
GET_ORDER_VERSION = 7
JST = timezone(timedelta(hours=9))


def enabled():
    return rms.load_keys() is not None


def _orders(d, date_type):
    keys = rms.load_keys()
    start = datetime.fromisoformat(d).replace(tzinfo=JST)
    numbers = rms.search(keys, start, start + timedelta(days=1, seconds=-1), date_type, ALL_PROGRESS)
    return rms.get_orders(keys, numbers, GET_ORDER_VERSION)


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
    ordered = _summarize(_orders(d, rms.DATE_ORDER))
    shipped = _summarize(_orders(d, rms.DATE_SHIP))
    return {
        "ordered": ordered[AMOUNT_FIELD],
        "qty": ordered["qty"],
        "count": ordered["count"],
        "shipped": shipped[AMOUNT_FIELD],
        "shipped_count": shipped["count"],
        "candidates": {f: ordered[f] for f in AMOUNT_FIELDS},
    }
