"""Yahoo!ショッピング ストアAPI(orderList / orderInfo)から、1日分の受注・発送を集計する。

- 認証: 環境変数 YAHOO_CLIENT_ID / YAHOO_CLIENT_SECRET / YAHOO_REFRESH_TOKEN / YAHOO_SELLER_ID
  (GitHub Secrets)。実行のたびにリフレッシュトークンからアクセストークンを取得する。
- 受注: 注文日時(OrderTimeFrom/To)で検索、キャンセル(OrderStatus=4)を除外
- 発送: 発送日(ShipDateFrom/To)で検索、キャンセルを除外
- 数量は orderList に無いため、受注ぶんだけ orderInfo を1件ずつ引いて Item/Quantity を合計する。
- 注文の中身(氏名・住所など)は取得フィールドに含めず、ログにも出さない。
"""
import os
import time
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

import requests

TOKEN_URL = "https://auth.login.yahoo.co.jp/yconnect/v2/token"
API = "https://circus.shopping.yahooapis.jp/ShoppingWebService/V1"
CANCELLED = "4"  # OrderStatus: 4=キャンセル
PAGE = 2000      # orderList の Result 上限
WAIT_SEC = 1.1   # どちらのAPIも 1 query/second
RETRIES = 5

_token = None


def enabled():
    return all(os.environ.get(k) for k in (
        "YAHOO_CLIENT_ID", "YAHOO_CLIENT_SECRET", "YAHOO_REFRESH_TOKEN", "YAHOO_SELLER_ID"))


def _access_token():
    global _token
    if _token:
        return _token
    r = requests.post(
        TOKEN_URL,
        auth=(os.environ["YAHOO_CLIENT_ID"], os.environ["YAHOO_CLIENT_SECRET"]),
        data={"grant_type": "refresh_token", "refresh_token": os.environ["YAHOO_REFRESH_TOKEN"]},
        timeout=30,
    )
    if r.status_code != 200:
        # エラー応答には error / error_description のみで、トークン値は含まれない。
        raise RuntimeError(f"yahoo token: HTTP {r.status_code} {r.text[:300]}")
    body = r.json()
    _token = body["access_token"]
    if body.get("refresh_token") and body["refresh_token"] != os.environ["YAHOO_REFRESH_TOKEN"]:
        # 値は出力しない。新しいトークンが返った事実だけ知らせる。
        print("yahoo: a NEW refresh_token was returned (not saved); update the YAHOO_REFRESH_TOKEN secret")
    return _token


def _post(path, xml_body):
    for attempt in range(1, RETRIES + 1):
        try:
            r = requests.post(
                f"{API}/{path}",
                headers={"Authorization": f"Bearer {_access_token()}",
                         "Content-Type": "text/xml; charset=utf-8"},
                data=xml_body.encode("utf-8"),
                timeout=60,
            )
        except (requests.Timeout, requests.ConnectionError) as e:
            if attempt == RETRIES:
                raise
            print(f"yahoo {path}: retry {attempt} after error {type(e).__name__}")
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 429 or r.status_code >= 500:
            if attempt == RETRIES:
                raise RuntimeError(f"yahoo {path}: HTTP {r.status_code}")
            print(f"yahoo {path}: retry {attempt} after HTTP {r.status_code}")
            time.sleep(2 ** attempt)
            continue
        if r.status_code != 200:
            raise RuntimeError(f"yahoo {path}: HTTP {r.status_code} {r.text[:300]}")
        time.sleep(WAIT_SEC)
        root = ET.fromstring(r.content)
        for el in root.iter():  # 名前空間を外す
            el.tag = el.tag.rsplit("}", 1)[-1]
        status = root.findtext(".//Status")
        if status not in (None, "OK"):
            raise RuntimeError(f"yahoo {path}: {root.findtext('.//Code')} {root.findtext('.//Message')}")
        return root


def _order_list(condition_xml):
    """条件に合う注文の [{OrderId, TotalPrice, OrderStatus}] を全ページ取得する。"""
    seller = escape(os.environ["YAHOO_SELLER_ID"])
    orders, start = [], 1
    while True:
        root = _post("orderList", (
            f"<Req><Search><Result>{PAGE}</Result><Start>{start}</Start></Search>"
            f"<Condition>{condition_xml}</Condition>"
            f"<Field>OrderId,TotalPrice,OrderStatus</Field>"
            f"<SellerId>{seller}</SellerId></Req>"))
        batch = root.findall(".//OrderInfo")
        for o in batch:
            orders.append({
                "id": o.findtext("OrderId"),
                "total": int(float(o.findtext("TotalPrice") or 0)),
                "status": (o.findtext("OrderStatus") or "").strip(),
            })
        total = int(root.findtext(".//TotalCount") or len(batch))
        start += PAGE
        if not batch or start > total:
            return [o for o in orders if o["status"] != CANCELLED]


def _quantity(order_id):
    root = _post("orderInfo", (
        f"<Req><Target><OrderId>{escape(order_id)}</OrderId><Field>OrderId,Quantity</Field></Target>"
        f"<SellerId>{escape(os.environ['YAHOO_SELLER_ID'])}</SellerId></Req>"))
    return sum(int(float(q.text or 0)) for q in root.findall(".//Item/Quantity"))


def fetch_day(d):
    """d: 'YYYY-MM-DD' (JST)。受注・発送の集計を返す(rakuten_rms.fetch_day と同じ形)。"""
    ymd = d.replace("-", "")
    ordered = _order_list(f"<OrderTimeFrom>{ymd}000000</OrderTimeFrom><OrderTimeTo>{ymd}235959</OrderTimeTo>")
    shipped = _order_list(f"<ShipDateFrom>{ymd}</ShipDateFrom><ShipDateTo>{ymd}</ShipDateTo>")
    return {
        "ordered": sum(o["total"] for o in ordered),
        "qty": sum(_quantity(o["id"]) for o in ordered),
        "count": len(ordered),
        "shipped": sum(o["total"] for o in shipped),
        "shipped_count": len(shipped),
    }
