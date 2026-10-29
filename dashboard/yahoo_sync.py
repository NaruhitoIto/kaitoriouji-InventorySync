"""このPC(注文APIに申請した固定IP)で定期実行し、ストアAPIの注文(ヤフーショッピング店・ヤフオク店)の集計値だけを
dashboard/data/yahoo_sales.json に書いてプッシュする。Actions の fetch_sales.py はこのファイルを読んで店舗8・6に反映する。

- タスクスケジューラ用の専用クローン(作業用のクローンとは別)で動かす。実行のたびに git pull --rebase する。
- API の呼び出しと認証は、在庫連携と同じ共通部品 yahoo_shopping_api(設定 yahoo_shopping_config.ini とトークン)。
  専用クローンには設定・トークンが無いので、--yahoo-config で作業用のクローンの設定ファイルを指定する。
  ログインし直す(4週間ごと)のは yahoo_auth.bat の1回だけでよい。
- ファイルには集計値と再認可の期限の目安だけを書く。注文の中身(氏名・住所など)は取得もしない。
- 数量のキャッシュ(注文ID → 数量)はリポジトリの外(%LOCALAPPDATA%)に置く。

集計の決まり:
- ストアAPIの注文には、ヤフーショッピング店とヤフオク店(ストアのオークション)の注文が両方入る。
  IsYahooAuctionOrder で分け、"shopping" と "auction" の2つの集計にする(ほとんどはヤフオク店)。
- 受注: 注文日時で検索、キャンセル(OrderStatus=4)を除外。発送: 出荷日で検索、キャンセルを除外
- 金額: モールクーポン・ポイントで値引きされる前の額(TotalPrice + TotalMallCouponDiscount + UsePoint)。
  楽天の totalPrice と同じ考え方で、ストアクリエイターProの注文CSVの「Sale Total」に当たる。
- 数量は注文検索APIに無いため、受注ぶんだけ注文詳細APIを1件ずつ引いて合計する。
  最終更新日時(LastUpdateTime)が変わっていない注文はキャッシュの数量を使う。

使い方: python dashboard/yahoo_sync.py [--days 4] [--no-git] [--yahoo-config C:\\InventorySync\\yahoo_shopping_config.ini]
タスクスケジューラからは pythonw.exe(ウィンドウを出さない)で --log <ファイル> を付けて実行する。
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # リポジトリ直下の共通部品

import yahoo_shopping_api as yapi  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_REL = "dashboard/data/yahoo_sales.json"
OUT = os.path.join(REPO, *OUT_REL.split("/"))
LOCAL_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "InventorySync-dashboard")
CACHE = os.path.join(LOCAL_DIR, "yahoo_qty_cache.json")
LOCK = os.path.join(LOCAL_DIR, "yahoo_sync.lock")
LOCK_STALE_SEC = 3 * 3600  # これより古いロックは異常終了の残りとみなす
JST = timezone(timedelta(hours=9))
FORMAT = 2              # days[日付] = {"shopping": 集計, "auction": 集計}。1 はショッピングとヤフオク店の合算だった
KEEP_DAYS = 60          # yahoo_sales.json と数量キャッシュに残す日数
WARN_DAYS = 5           # 再認可の期限がこの日数以内なら警告
CANCELLED = "4"         # OrderStatus: 4=キャンセル
WAIT_SEC = 1.1          # 注文APIは 1 query/second
# 注文検索は注文日時の指定が必須で、出荷日だけでは検索できない。
# 出荷日で数えるときは、注文日時をこの日数ぶん遡った範囲と組み合わせる(予約注文も拾えるよう長めに)。
SHIP_LOOKBACK_DAYS = 180
FIELDS = "OrderId,TotalPrice,TotalMallCouponDiscount,UsePoint,OrderStatus,LastUpdateTime,IsYahooAuctionOrder"


def git(*args, check=True):
    # pythonw から呼んでも git のコンソールウィンドウが開かないようにする
    return subprocess.run(["git", "-C", REPO, *args], check=check, capture_output=True, text=True,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def num(o, k):
    return int(float(o.get(k) or 0))


def orders(ycfg, start, end, condition=""):
    """注文日時が start〜end で condition に合う、キャンセル以外の注文の [{id, total(値引き前), updated, auction}]。"""
    rows = yapi.search_orders(ycfg, start, end, condition, FIELDS)
    time.sleep(WAIT_SEC)
    return [{"id": o["OrderId"],
             "total": num(o, "TotalPrice") + num(o, "TotalMallCouponDiscount") + num(o, "UsePoint"),
             "updated": o.get("LastUpdateTime", ""),
             "auction": o.get("IsYahooAuctionOrder") == "true"}
            for o in rows if o.get("OrderStatus") != CANCELLED]


def quantity(ycfg, o, cache, d):
    """注文の数量の合計。cache: {OrderId: {"updated", "qty", "date"}}。更新されていない注文は注文詳細APIを呼ばない。"""
    c = cache.get(o["id"])
    if not c or c["updated"] != o["updated"]:
        qty = sum(int(float(i.get("Quantity") or 0)) for i in yapi.order_lines(ycfg, o["id"]))
        time.sleep(WAIT_SEC)
        c = cache[o["id"]] = {"updated": o["updated"], "qty": qty, "date": d}
    return c["qty"]


def fetch_day(ycfg, d, cache):
    """d: 'YYYY-MM-DD'(JST)。{"shopping": 集計, "auction": 集計} を返す。
    集計はどちらも rakuten_sales.fetch_day と同じ形({ordered, qty, count, shipped, shipped_count})。"""
    day = date.fromisoformat(d)
    start = datetime(day.year, day.month, day.day, tzinfo=JST)
    end = start + timedelta(days=1, seconds=-1)
    ordered = orders(ycfg, start, end)
    shipped = orders(ycfg, start - timedelta(days=SHIP_LOOKBACK_DAYS), end,
                     f"<ShipDateFrom>{day:%Y%m%d}</ShipDateFrom><ShipDateTo>{day:%Y%m%d}</ShipDateTo>")
    out = {}
    for kind, auction in (("shopping", False), ("auction", True)):
        o_ = [o for o in ordered if o["auction"] == auction]
        s_ = [o for o in shipped if o["auction"] == auction]
        out[kind] = {
            "ordered": sum(o["total"] for o in o_),
            "qty": sum(quantity(ycfg, o, cache, d) for o in o_),
            "count": len(o_),
            "shipped": sum(o["total"] for o in s_),
            "shipped_count": len(s_),
        }
    return out


def push():
    git("add", OUT_REL)
    if git("diff", "--staged", "--quiet", check=False).returncode == 0:
        print("no change to push")
        return
    git("commit", "-m", "Update Yahoo sales data")
    for attempt in range(1, 4):
        if git("push", check=False).returncode == 0:
            print("pushed")
            return
        print(f"push rejected, rebase and retry {attempt}")
        git("pull", "--rebase")
    raise RuntimeError("git push failed")


def acquire_lock():
    """手動実行(過去分の取得など)と定期実行が同じクローンで重ならないようにする。"""
    os.makedirs(LOCAL_DIR, exist_ok=True)
    try:
        if time.time() - os.path.getmtime(LOCK) > LOCK_STALE_SEC:
            os.remove(LOCK)
    except FileNotFoundError:
        pass
    try:
        os.close(os.open(LOCK, os.O_CREAT | os.O_EXCL))
    except FileExistsError:
        sys.exit("yahoo: 前の実行がまだ終わっていないため、今回は見送ります")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=4, help="今日から遡って取得する日数")
    ap.add_argument("--no-git", action="store_true", help="pull / commit / push をしない(試験用)")
    ap.add_argument("--log", help="出力をこのファイルに追記する(タスクスケジューラ用)")
    ap.add_argument("--yahoo-config", help="yahoo_shopping_config.ini のパス(省略時はこのリポジトリ直下のもの)")
    args = ap.parse_args()
    if args.log:
        sys.stdout = sys.stderr = open(args.log, "a", encoding="utf-8", buffering=1)
    print(f"--- {datetime.now(JST):%Y-%m-%d %H:%M} yahoo_sync --days {args.days}")
    acquire_lock()
    try:
        run(args)
    finally:
        os.remove(LOCK)


def run(args):
    try:
        ycfg = yapi.load_config(config_file=args.yahoo_config)
    except SystemExit as e:
        sys.exit(f"yahoo: {e}")

    if not args.no_git:
        git("pull", "--rebase")

    deadline = yapi.reauth_deadline(ycfg)
    expires = datetime.fromtimestamp(deadline, JST).date().isoformat() if deadline else None
    if expires and date.fromisoformat(expires) - date.today() <= timedelta(days=WARN_DAYS):
        print(f"WARNING yahoo: 再認可の期限 {expires} が近いです。yahoo_auth.bat でログインし直してください")

    doc = load_json(OUT, {"days": {}})
    if doc.get("format") != FORMAT:
        doc = {"days": {}}  # 古い形の日は使わない(取り直す)
    cache = load_json(CACHE, {})
    today = datetime.now(JST).date()
    started = time.time()
    for i in range(args.days - 1, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        try:
            doc["days"][d] = fetch_day(ycfg, d, cache)
        except (Exception, SystemExit) as e:
            # 認可切れ(px-04102、トークンの更新に失敗)は全日で失敗するので打ち切る。前回の値はそのまま残る。
            print(f"yahoo {d}: failed ({e})")
            if isinstance(e, SystemExit) or "px-04102" in str(e):
                print("yahoo: 認可が切れています。yahoo_auth.bat でログインし直してください")
                save_json(CACHE, cache)
                sys.exit(1)
            continue
        print(f"yahoo {d}: {json.dumps(doc['days'][d], ensure_ascii=False)}")

    cutoff = (today - timedelta(days=KEEP_DAYS)).isoformat()
    days = {d: v for d, v in sorted(doc["days"].items()) if d >= cutoff}
    generated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    save_json(OUT, {"format": FORMAT, "generated_at": generated_at, "auth_expires": expires, "days": days})
    save_json(CACHE, {k: v for k, v in cache.items() if v["date"] >= cutoff})
    print(f"wrote {len(days)} day(s) in {time.time() - started:.0f}s")

    if not args.no_git:
        push()


if __name__ == "__main__":
    main()
