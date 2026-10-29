import os
import time
import json
from datetime import datetime, timedelta, timezone

import requests

import rakuten_rms
import yahoo_store

BASE = "https://world-switch.com/kaitoriouji/ext"
# この日付以降は該当店舗をモールAPIの値で差し替える。
# それより前の日付はWASABIの値を残し、APIの値はログに比較表示するだけ。
RAKUTEN_FROM = os.environ.get("RAKUTEN_FROM") or "2026-10-17"
# ヤフーショッピング(id:8)。切替日を決めるまでは未来日=比較ログのみ。ヤフオク(id:6)はWASABIのまま。
YAHOO_FROM = os.environ.get("YAHOO_FROM") or "9999-12-31"
# (ログ名, モジュール, WASABIのshop_id, 置き換え開始日)
API_SOURCES = [
    ("rakuten", rakuten_rms, 1, RAKUTEN_FROM),
    ("yahoo", yahoo_store, 8, YAHOO_FROM),
]
AUTH = os.environ["WASABI_AUTH"]
HEADERS = {"Authorization": f"Bearer {AUTH}"}
SHOP_IDS = [1, 4, 5, 6, 8, 11, 20, 22, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34]
WINDOW_DAYS = int(os.environ.get("WINDOW_DAYS") or 4)
JST = timezone(timedelta(hours=9))
RETRIES = 5


def request_with_retry(method, url, **kwargs):
    """Retry on timeouts, connection errors, 429 and 5xx with exponential backoff."""
    for attempt in range(1, RETRIES + 1):
        try:
            r = requests.request(method, url, headers=HEADERS, **kwargs)
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.HTTPError(f"{r.status_code} for {url}", response=r)
            r.raise_for_status()
            return r
        except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status is not None and status < 500 and status != 429:
                raise  # 4xx other than 429: retrying will not help
            if attempt == RETRIES:
                raise
            wait = 2 ** attempt
            print(f"retry {attempt}/{RETRIES - 1} after {wait}s: {e}")
            time.sleep(wait)


def daterange():
    today = datetime.now(JST).date()
    return [(today - timedelta(days=i)).isoformat() for i in range(WINDOW_DAYS)][::-1]


def fetch_totals(endpoint, start, end):
    r = request_with_retry(
        "GET",
        f"{BASE}/api/total/{endpoint}",
        params={"start_date": start, "end_date": end},
        timeout=30,
    )
    return {row["date"]: row for row in r.json()}


def fetch_qty(d):
    qty_by_shop = {}
    count_by_shop = {}
    total_qty = 0
    page = 1
    while True:
        r = request_with_retry(
            "POST",
            f"{BASE}/api/orders/search",
            json={
                "limit": 1000,
                "page": page,
                "query": {"order_date": {"gte": f"{d} 00:00:00", "lte": f"{d} 23:59:59"}},
            },
            timeout=60,
        )
        payload = r.json()
        for o in payload["data"]:
            shop = int(o["shop_id"])
            count_by_shop[shop] = count_by_shop.get(shop, 0) + 1
            for od in o.get("order_details", []):
                q = int(float(od.get("quantity", 0)))
                qty_by_shop[shop] = qty_by_shop.get(shop, 0) + q
                total_qty += q
        if len(payload["data"]) < 1000:
            break
        page += 1
        time.sleep(0.4)
    return total_qty, qty_by_shop, count_by_shop


def apply_api_day(name, doc, shop_id, from_date, d, r):
    """モールAPIの1日分(r)で、docの該当店舗とその合計を差し替える。from_date より前は比較ログのみ。"""
    s = next(x for x in doc["shops"] if x["id"] == shop_id)
    if d < from_date:
        print(f"{name} verify {d}: WASABI ordered={s['ordered']} count={s.get('count')} qty={s['qty']} "
              f"shipped={s['shipped']} | API {r.get('candidates', r['ordered'])} count={r['count']} "
              f"qty={r['qty']} shipped={r['shipped']}")
        return
    doc["ordered_total"] += r["ordered"] - s["ordered"]
    doc["ordered_qty"] += r["qty"] - s["qty"]
    doc["ordered_count"] += r["count"] - s.get("count", 0)
    if s["shipped"] == 0:
        doc["shipped_count"] += r["shipped_count"]
    doc["shipped_total"] += r["shipped"] - s["shipped"]
    s.update(ordered=r["ordered"], qty=r["qty"], count=r["count"], shipped=r["shipped"])
    print(f"{name} {d}: ordered={r['ordered']} count={r['count']} qty={r['qty']} shipped={r['shipped']}")


def main():
    dates = daterange()
    ordered = fetch_totals("ordered", dates[0], dates[-1])
    shipped = fetch_totals("shipped", dates[0], dates[-1])

    docs = {}
    for d in dates:
        o = ordered.get(d)
        s = shipped.get(d)
        if not o or not s:
            continue
        total_qty, qty_by_shop, count_by_shop = fetch_qty(d)
        o_shop = {x["shop_id"]: int(x["total"]) for x in o["shops"]}
        s_shop = {x["shop_id"]: int(x["total"]) for x in s["shops"]}
        shops = [
            {
                "id": sid,
                "ordered": o_shop.get(sid, 0),
                "shipped": s_shop.get(sid, 0),
                "qty": qty_by_shop.get(sid, 0),
                "count": count_by_shop.get(sid, 0),
            }
            for sid in SHOP_IDS
        ]
        docs[d] = {
            "date": d,
            "ordered_total": int(o["total"]),
            "ordered_count": int(o["total_order"]),
            "ordered_qty": total_qty,
            "shipped_total": int(s["total"]),
            "shipped_count": int(s["total_order"]),
            "shops": shops,
        }

    for name, module, shop_id, from_date in API_SOURCES:
        if not module.enabled():
            print(f"{name}: credentials not set, using WASABI values")
            continue
        for d, doc in docs.items():
            try:
                r = module.fetch_day(d)
            except Exception as e:
                # 失敗した日はWASABIの値のまま(ダッシュボードを悪化させない)
                print(f"{name} {d}: skipped ({e})")
                continue
            apply_api_day(name, doc, shop_id, from_date, d, r)

    out = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "docs": docs,
    }
    os.makedirs("data", exist_ok=True)
    with open("data/latest_sales.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"Wrote {len(docs)} day(s) to data/latest_sales.json")


if __name__ == "__main__":
    main()
