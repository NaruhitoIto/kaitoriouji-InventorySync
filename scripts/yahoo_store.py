"""Yahoo!ショッピング ストアAPI(orderList / orderInfo)から、1日分の受注・発送を集計する。

- ストアAPIには、ヤフーショッピング店の注文とヤフオク店(ストアのオークション)の注文が両方入る。
  IsYahooAuctionOrder で分け、"shopping" と "auction" の2つの集計を返す(ほとんどはヤフオク店)。

- 認証: 環境変数 YAHOO_CLIENT_ID / YAHOO_CLIENT_SECRET / YAHOO_REFRESH_TOKEN / YAHOO_SELLER_ID
  (申請した固定IPのPCの環境変数)。実行のたびにリフレッシュトークンからアクセストークンを取得する。
- 公開鍵認証: YAHOO_PUBLIC_KEY_PATH(ストアクリエイターPro > 設定 > 暗号鍵管理 の公開鍵を保存したファイル)と
  YAHOO_PUBLIC_KEY_VERSION があれば X-sws-signature を付ける。付けないと注文APIは認可から12時間で
  px-04102 になり、付けると再認可は4週間ごとでよい。
- 受注: 注文日時(OrderTimeFrom/To)で検索、キャンセル(OrderStatus=4)を除外
- 発送: 出荷日(ShipDateFrom/To)で検索、キャンセルを除外
- 金額: モールクーポン・ポイントで値引きされる前の額(TotalPrice + TotalMallCouponDiscount + UsePoint)。
  楽天の totalPrice と同じ考え方で、ストアクリエイターProの注文CSVの「Sale Total」に当たる。
- 数量は orderList に無いため、受注ぶんだけ orderInfo を1件ずつ引いて Item/Quantity を合計する。
  qty_cache を渡すと、最終更新日時(LastUpdateTime)が変わっていない注文は引き直さない。
- 注文の中身(氏名・住所など)は取得フィールドに含めず、ログにも出さない。

試験: python scripts/yahoo_store.py 2026-10-28
"""
import base64
import json
import os
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from xml.sax.saxutils import escape

import requests

TOKEN_URL = "https://auth.login.yahoo.co.jp/yconnect/v2/token"
API = "https://circus.shopping.yahooapis.jp/ShoppingWebService/V1"
CANCELLED = "4"  # OrderStatus: 4=キャンセル
PAGE = 2000      # orderList の Result 上限
WAIT_SEC = 1.1   # どちらのAPIも 1 query/second
RETRIES = 5
# orderList は OrderTime 系の指定が必須で、出荷日(ShipDate)だけでは検索できない。
# 出荷日で数えるときは、注文日時をこの日数ぶん遡った範囲と組み合わせる(予約注文も拾えるよう長めに)。
SHIP_LOOKBACK_DAYS = 180

_token = None
_public_key = None
_auth_status_logged = False


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


def _signature_headers():
    """公開鍵認証のヘッダー。「ストアアカウント:UNIX時刻」を公開鍵(RSA/PKCS1v15)で暗号化してBase64。
    時刻は10分以内である必要があるため、リクエストごとに作る。"""
    global _public_key
    path = os.environ.get("YAHOO_PUBLIC_KEY_PATH")
    if not path:
        return {}
    if _public_key is None:
        from cryptography.hazmat.primitives import serialization
        with open(path, "rb") as f:
            _public_key = serialization.load_pem_public_key(f.read())
    from cryptography.hazmat.primitives.asymmetric import padding
    plain = f"{os.environ['YAHOO_SELLER_ID']}:{int(time.time())}".encode("utf-8")
    return {
        "X-sws-signature": base64.b64encode(_public_key.encrypt(plain, padding.PKCS1v15())).decode("ascii"),
        "X-sws-signature-version": os.environ.get("YAHOO_PUBLIC_KEY_VERSION") or "1",
    }


def _post(path, xml_body):
    global _auth_status_logged
    for attempt in range(1, RETRIES + 1):
        try:
            r = requests.post(
                f"{API}/{path}",
                headers={"Authorization": f"Bearer {_access_token()}",
                         "Content-Type": "text/xml; charset=utf-8",
                         **_signature_headers()},
                data=xml_body.encode("utf-8"),
                timeout=60,
            )
        except (requests.Timeout, requests.ConnectionError) as e:
            if attempt == RETRIES:
                raise
            print(f"yahoo {path}: retry {attempt} after error {type(e).__name__}")
            time.sleep(2 ** attempt)
            continue
        # 公開鍵認証の結果(authorized / unauthorized / expired-key-version など)を最初の1回だけ出す
        if not _auth_status_logged and os.environ.get("YAHOO_PUBLIC_KEY_PATH"):
            print(f"yahoo public key: {r.headers.get('X-SWS-Authorize-Status', '(no status header)')}")
            _auth_status_logged = True
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
    """条件に合う注文の [{id, total(値引き前), status, updated, auction}] を全ページ取得する。"""
    seller = escape(os.environ["YAHOO_SELLER_ID"])
    num = lambda o, k: int(float(o.findtext(k) or 0))
    orders, start = [], 1
    while True:
        root = _post("orderList", (
            f"<Req><Search><Result>{PAGE}</Result><Start>{start}</Start>"
            f"<Condition>{condition_xml}</Condition>"
            f"<Field>OrderId,TotalPrice,TotalMallCouponDiscount,UsePoint,OrderStatus,LastUpdateTime,IsYahooAuctionOrder</Field></Search>"
            f"<SellerId>{seller}</SellerId></Req>"))
        batch = root.findall(".//OrderInfo")
        for o in batch:
            orders.append({
                "id": o.findtext("OrderId"),
                "total": num(o, "TotalPrice") + num(o, "TotalMallCouponDiscount") + num(o, "UsePoint"),
                "status": (o.findtext("OrderStatus") or "").strip(),
                "updated": o.findtext("LastUpdateTime") or "",
                "auction": (o.findtext("IsYahooAuctionOrder") or "").strip() == "true",
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


def _cached_quantity(o, qty_cache, d):
    """qty_cache: {OrderId: {"updated", "qty", "date"}}。更新されていない注文は orderInfo を呼ばない。"""
    if qty_cache is None:
        return _quantity(o["id"])
    c = qty_cache.get(o["id"])
    if not c or c["updated"] != o["updated"]:
        c = qty_cache[o["id"]] = {"updated": o["updated"], "qty": _quantity(o["id"]), "date": d}
    return c["qty"]


def fetch_day(d, qty_cache=None):
    """d: 'YYYY-MM-DD' (JST)。{"shopping": 集計, "auction": 集計} を返す。
    集計はどちらも rakuten_rms.fetch_day と同じ形({ordered, qty, count, shipped, shipped_count})。"""
    ymd = d.replace("-", "")
    since = (date.fromisoformat(d) - timedelta(days=SHIP_LOOKBACK_DAYS)).strftime("%Y%m%d")
    ordered = _order_list(f"<OrderTimeFrom>{ymd}000000</OrderTimeFrom><OrderTimeTo>{ymd}235959</OrderTimeTo>")
    shipped = _order_list(f"<OrderTimeFrom>{since}000000</OrderTimeFrom><OrderTimeTo>{ymd}235959</OrderTimeTo>"
                          f"<ShipDateFrom>{ymd}</ShipDateFrom><ShipDateTo>{ymd}</ShipDateTo>")
    out = {}
    for kind, auction in (("shopping", False), ("auction", True)):
        o_ = [o for o in ordered if o["auction"] == auction]
        s_ = [o for o in shipped if o["auction"] == auction]
        out[kind] = {
            "ordered": sum(o["total"] for o in o_),
            "qty": sum(_cached_quantity(o, qty_cache, d) for o in o_),
            "count": len(o_),
            "shipped": sum(o["total"] for o in s_),
            "shipped_count": len(s_),
        }
    return out


if __name__ == "__main__":
    if not enabled():
        sys.exit("YAHOO_CLIENT_ID / YAHOO_CLIENT_SECRET / YAHOO_REFRESH_TOKEN / YAHOO_SELLER_ID が未設定です")
    for day in sys.argv[1:]:
        print(day, json.dumps(fetch_day(day), ensure_ascii=False))
