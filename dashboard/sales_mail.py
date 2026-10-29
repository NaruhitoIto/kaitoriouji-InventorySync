"""売上ダッシュボードの値から、当月の進捗メール(昨日/累計/先読み)の件名と本文を作る。

入力はダッシュボード(Artifact)のデータベースを書き出したフォルダ:
  <フォルダ>/sales_daily/YYYY-MM-DD.json       {date, ordered_total, shops:[{id, ordered}, ...]}
  <フォルダ>/manual_overrides/YYYY-MM-DD.json  {date, shops:[{id, ordered}, ...]}(無くてもよい)
ほかの項目は使わない。手動取込(manual_overrides)はダッシュボードの画面と同じように、その店舗の値を置き換える。

集計の決まり(ダッシュボードの「受注日ベース」と同じ値):
- 昨日: 配信日の前日。累計: 前日の月の1日から前日まで。月初(1日)の配信は前月の締めになる。
- 先読み: 累計 × その月の日数 ÷ 前日までの日数(四捨五入)。
- 合計は全店舗(下の表に無い店舗も含む)。表に無い店舗に売上があれば「その他」の行を出す。

日付が抜けている、または各日の店舗の合計が ordered_total と合わないときは、メールを作らずに終了コード1で止める。

使い方: python dashboard/sales_mail.py <フォルダ> [--date YYYY-MM-DD(配信日。省略時は今日・日本時間)] [--json]
"""
import argparse
import calendar
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))
SUBJECT = "{m}/{d}買取王子:昨日/累計/先読み"
# (メールの表示名, WASABI の shop_id)。ダッシュボードの店舗名: 1 楽天市場店・32 楽天SKU、5 Amazon店、
# 6 ヤフオク店・28 併売ヤフオク・29 ヤフオク1・30 ヤフオク2、8 ヤフーショッピング店、22 ラクマ、
# 25〜27 自社店舗、31 メルカリ。
MALLS = [
    ("Amazon", [5]),
    ("ヤフオク", [6, 28, 29, 30]),
    ("楽天", [1, 32]),
    ("メルカリ", [31]),
    ("ラクマ", [22]),
    ("自社サイト", [25, 26, 27]),
    ("Yahooｼｮｯﾋﾟﾝｸﾞ", [8]),
]
OTHER = "その他"


def load_days(folder):
    days = {}
    for f in sorted((folder / "sales_daily").glob("*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        days[d["date"]] = d
    overrides = {}
    for f in sorted((folder / "manual_overrides").glob("*.json")):
        o = json.loads(f.read_text(encoding="utf-8"))
        overrides[o["date"]] = o
    return days, overrides


def shop_values(day, override):
    """{shop_id: 受注金額}。手動取込があれば、その店舗の値を置き換える。"""
    shops = {int(s["id"]): int(s.get("ordered") or 0) for s in day["shops"]}
    total = sum(shops.values())
    if total != int(day["ordered_total"]):
        raise ValueError(f"{day['date']}: 店舗の合計 {total:,} が ordered_total {int(day['ordered_total']):,} と合いません")
    for s in (override or {}).get("shops", []):
        if isinstance(s.get("ordered"), (int, float)):
            shops[int(s["id"])] = int(s["ordered"])
    return shops


def forecast(total, days_in_month, elapsed):
    return (total * days_in_month * 2 + elapsed) // (elapsed * 2)  # 四捨五入


def build(folder, send_date):
    days, overrides = load_days(folder)
    yesterday = send_date - timedelta(days=1)
    first = yesterday.replace(day=1)
    dates = [(first + timedelta(days=i)).isoformat() for i in range(yesterday.day)]
    missing = [d for d in dates if d not in days]
    if missing:
        raise ValueError(f"ダッシュボードにない日があります: {', '.join(missing)}")

    named = {sid for _, ids in MALLS for sid in ids}
    rows = [(name, ids) for name, ids in MALLS]
    cum = {name: 0 for name, _ in rows}
    cum[OTHER] = 0
    last = {}
    for d in dates:
        shops = shop_values(days[d], overrides.get(d))
        sums = {name: sum(shops.get(i, 0) for i in ids) for name, ids in rows}
        sums[OTHER] = sum(v for sid, v in shops.items() if sid not in named)
        for name, v in sums.items():
            cum[name] += v
        last = sums
    if cum[OTHER]:
        rows.append((OTHER, []))

    n_days = calendar.monthrange(yesterday.year, yesterday.month)[1]
    elapsed = yesterday.day

    def line(y, c):
        return y, c, forecast(c, n_days, elapsed)

    total = line(sum(last.values()), sum(cum.values()))
    body = [f"■売上金額:昨日{total[0]:,}円 / 累計{total[1]:,}円 / 先読み{total[2]:,}円", "", "■売上金額"]
    for name, _ in rows:
        y, c, f = line(last[name], cum[name])
        body += [f"--{name}--", f"{y:,}円  / {c:,}円  / {f:,}円", ""]
    subject = SUBJECT.format(m=send_date.month, d=send_date.day)
    return subject, "\n".join(body).rstrip() + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", type=Path)
    ap.add_argument("--date", help="配信日 YYYY-MM-DD(省略時は今日・日本時間)")
    ap.add_argument("--json", action="store_true", help='{"subject", "body"} の JSON で出す')
    args = ap.parse_args()
    send_date = date.fromisoformat(args.date) if args.date else datetime.now(JST).date()
    try:
        subject, body = build(args.folder, send_date)
    except ValueError as e:
        sys.exit(f"ERROR: {e}")
    sys.stdout.reconfigure(encoding="utf-8")
    if args.json:
        print(json.dumps({"subject": subject, "body": body}, ensure_ascii=False))
    else:
        print(subject)
        print()
        print(body, end="")


if __name__ == "__main__":
    main()
