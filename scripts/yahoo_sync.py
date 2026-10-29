"""このPC(注文APIに申請した固定IP)で定期実行し、Yahoo!ショッピング(店舗8)の集計値だけを
data/yahoo_sales.json に書いてプッシュする。Actions の fetch_sales.py はこのファイルを読んで店舗8に反映する。

- タスクスケジューラ用の専用クローン(作業用のクローンとは別)で動かす。実行のたびに git pull --rebase する。
- 認証情報はユーザー環境変数(yahoo_auth.py で設定)。ファイルには集計値と再認可の期限の目安だけを書く。
- 数量のキャッシュ(注文ID → 数量)はリポジトリの外(%LOCALAPPDATA%)に置く。

使い方: python scripts/yahoo_sync.py [--days 4] [--no-git]
タスクスケジューラからは pythonw.exe(ウィンドウを出さない)で --log <ファイル> を付けて実行する。
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone

import yahoo_store
from yahoo_auth import user_env

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(REPO, "data", "yahoo_sales.json")
LOCAL_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "wasabi-dashboard-sync")
CACHE = os.path.join(LOCAL_DIR, "yahoo_qty_cache.json")
LOCK = os.path.join(LOCAL_DIR, "yahoo_sync.lock")
LOCK_STALE_SEC = 3 * 3600  # これより古いロックは異常終了の残りとみなす
JST = timezone(timedelta(hours=9))
KEEP_DAYS = 60          # yahoo_sales.json と数量キャッシュに残す日数
AUTH_DAYS = 28          # 公開鍵認証ありの再認可間隔
WARN_DAYS = 5           # 再認可の期限がこの日数以内なら警告
ENV_NAMES = ("YAHOO_CLIENT_ID", "YAHOO_CLIENT_SECRET", "YAHOO_REFRESH_TOKEN", "YAHOO_SELLER_ID",
             "YAHOO_PUBLIC_KEY_PATH", "YAHOO_PUBLIC_KEY_VERSION", "YAHOO_AUTHORIZED_AT")


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


def push():
    git("add", "data/yahoo_sales.json")
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
    # ターミナルを開いた後に yahoo_auth.py で更新した値も使えるよう、レジストリから読み直す
    for name in ENV_NAMES:
        value = user_env(name)
        if value:
            os.environ[name] = value
    if not yahoo_store.enabled():
        sys.exit("yahoo: 環境変数が未設定です(python scripts/yahoo_auth.py を実行してください)")

    if not args.no_git:
        git("pull", "--rebase")

    authorized = os.environ.get("YAHOO_AUTHORIZED_AT")
    expires = (date.fromisoformat(authorized) + timedelta(days=AUTH_DAYS)).isoformat() if authorized else None
    if expires and date.fromisoformat(expires) - date.today() <= timedelta(days=WARN_DAYS):
        print(f"WARNING yahoo: 再認可の期限 {expires} が近いです。python scripts/yahoo_auth.py を実行してください")

    doc = load_json(OUT, {"days": {}})
    cache = load_json(CACHE, {})
    today = datetime.now(JST).date()
    started = time.time()
    for i in range(args.days - 1, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        try:
            doc["days"][d] = yahoo_store.fetch_day(d, cache)
        except Exception as e:
            # 認可切れ(px-04102)などは全日で失敗するので打ち切る。前回の値はそのまま残る。
            print(f"yahoo {d}: failed ({e})")
            if "px-04102" in str(e) or "yahoo token" in str(e):
                print("yahoo: 認可が切れています。python scripts/yahoo_auth.py で再認可してください")
                save_json(CACHE, cache)
                sys.exit(1)
            continue
        print(f"yahoo {d}: {json.dumps(doc['days'][d], ensure_ascii=False)}")

    cutoff = (today - timedelta(days=KEEP_DAYS)).isoformat()
    days = {d: v for d, v in sorted(doc["days"].items()) if d >= cutoff}
    generated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    save_json(OUT, {"generated_at": generated_at, "auth_expires": expires, "days": days})
    save_json(CACHE, {k: v for k, v in cache.items() if v["date"] >= cutoff})
    print(f"wrote {len(days)} day(s) in {time.time() - started:.0f}s")

    if not args.no_git:
        push()


if __name__ == "__main__":
    main()
