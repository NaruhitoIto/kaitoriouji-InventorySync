"""ストアAPIの注文の1日分を data/yahoo_sales.json から返す。
source("shopping")(ヤフーショッピング店)と source("auction")(ヤフオク店)が、rakuten_sales と同じ enabled / fetch_day の形。

注文APIは申請した固定IPからしか呼べないため、Actions では呼ばない。
そのPCの定期実行(dashboard/yahoo_sync.py)が集計値だけをこのファイルに書いてプッシュする。
"""
import json
import os
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "yahoo_sales.json")
FORMAT = 2       # yahoo_sync.py の FORMAT と同じ。違う形(合算だった 1)のファイルは使わない
STALE_HOURS = 3  # これより古ければ、そのPCの定期実行が止まっている可能性をログに出す
WARN_DAYS = 5    # 再認可の期限がこの日数以内ならログに出す

_doc = None


def _load():
    global _doc
    if _doc is None:
        with open(PATH, encoding="utf-8") as f:
            _doc = json.load(f)
        if _doc.get("format") != FORMAT:
            print(f"yahoo: {PATH} is an old format, not used until the fixed-IP PC rewrites it")
            _doc = {"days": {}}
            return _doc
        generated = datetime.fromisoformat(_doc["generated_at"].replace("Z", "+00:00"))
        age_h = (datetime.now(timezone.utc) - generated).total_seconds() / 3600
        if age_h > STALE_HOURS:
            print(f"yahoo: {PATH} is {age_h:.1f}h old (is the scheduled task on the fixed-IP PC running?)")
        expires = _doc.get("auth_expires")
        if expires and date.fromisoformat(expires) - date.today() <= timedelta(days=WARN_DAYS):
            print(f"yahoo: re-authorization due by {expires} (run yahoo_auth.bat on the fixed-IP PC)")
    return _doc


def enabled():
    return os.path.exists(PATH)


def source(kind):
    """kind: "shopping" か "auction"。fetch_sales.py の API_SOURCES に渡す。"""
    def fetch_day(d):
        day = (_load()["days"].get(d) or {}).get(kind)
        if day is None:
            raise KeyError(f"no {kind} data for {d} in {PATH}")
        return day
    return SimpleNamespace(enabled=enabled, fetch_day=fetch_day)
