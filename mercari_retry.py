# -*- coding: utf-8 -*-
"""
メルカリShopsの在庫更新ファイルがエラーになったとき、エラー行だけを除いたファイルを作り直すツール。
（メルカリは1行でもエラーがあるとファイル全体を取り込まないため）

使い方:
  メルカリの取り込み結果（product_update_import_result_*.csv）を mercari_error_retry.bat にドラッグ＆ドロップする。
  複数まとめてドロップしてもよい。ダブルクリックした場合は、ダウンロードフォルダの一番新しい取り込み結果を使う。

動き:
  1. 取り込み結果から「失敗」した行（SKU1_ID）と「成功」した行を読む
  2. todo/ 直下に残っている在庫更新ファイルの行と、取り込み結果の成功行を合わせ、失敗した行を除く
  3. 1つの在庫更新ファイル（5項目・Shift_JIS）として todo/ に作る
  4. 元の在庫更新ファイルは todo/エラー除外前/ に移す（10分ごとの結合でエラー行が戻らないように）
  5. 失敗した行の一覧（商品管理コード・エラー理由）を todo/参考/ に出す
"""
import csv
import io
import sys
import time
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
TODO_DIR = BASE_DIR / "todo"
BEFORE_DIR = TODO_DIR / "エラー除外前"
REFERENCE_DIR = TODO_DIR / "参考"
LOCK_FILE = BASE_DIR / "cross_mall_sync.lock"  # 10分ごとの処理（cross_mall_sync.py）と同時に動かないようにする
DOWNLOADS = Path.home() / "Downloads"
UPLOAD_GLOB = "メルカリ_在庫更新_アップロード_*.csv"
HEADER = ["商品ID", "SKU1_ID", "SKU1_現在の在庫数", "SKU1_増減フラグ", "SKU1_在庫増減数"]
ENC = "cp932"


def read_csv(path):
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode(ENC, errors="replace")
    return list(csv.reader(io.StringIO(text)))


def read_result(path):
    """取り込み結果を読み、(失敗 {SKU1_ID: (商品管理コード, 商品ID, 現在の在庫数, エラー理由)}, 成功行 [5項目]) を返す。"""
    rows = read_csv(path)
    h = rows[0]
    need = ("処理結果", "エラー理由", "商品ID", "SKU1_ID", "SKU1_現在の在庫数")
    missing = [n for n in need if n not in h]
    if missing:
        raise SystemExit(f"{path.name} はメルカリの在庫更新の取り込み結果ではないようです（列がありません: {missing}）。"
                         "「一括登録」ではなく「一括編集」の結果ファイルを使ってください")
    i = {n: h.index(n) for n in need}
    i_code = h.index("SKU1_商品管理コード") if "SKU1_商品管理コード" in h else None
    failed, ok = {}, []
    for r in rows[1:]:
        if len(r) <= max(i.values()):
            continue
        sku_id = r[i["SKU1_ID"]].strip()
        if not sku_id:
            continue
        reason = r[i["エラー理由"]].strip().replace("\r", "").replace("\n", " / ")
        if reason or r[i["処理結果"]].strip() != "成功":
            code = r[i_code].strip() if i_code is not None and len(r) > i_code else ""
            failed[sku_id] = (code, r[i["商品ID"]].strip(), r[i["SKU1_現在の在庫数"]].strip(), reason or r[i["処理結果"]])
        else:
            cur = r[i["SKU1_現在の在庫数"]].strip()
            ok.append([r[i["商品ID"]].strip(), sku_id, cur, "2", cur])
    return failed, ok


def acquire_lock():
    for _ in range(30):  # 10分ごとの処理が動いていれば、最大1分待つ
        if not LOCK_FILE.exists() or time.time() - LOCK_FILE.stat().st_mtime > 2 * 3600:
            LOCK_FILE.write_text("mercari_retry", encoding="utf-8")
            return True
        time.sleep(2)
    return False


def main():
    args = [Path(a) for a in sys.argv[1:] if a.strip()]
    if not args:
        found = sorted(DOWNLOADS.glob("product_update_import_result*.csv"), key=lambda p: p.stat().st_mtime)
        if not found:
            raise SystemExit("取り込み結果ファイルが指定されていません（ダウンロードフォルダにも見つかりません）")
        args = [found[-1]]
        print(f"ダウンロードフォルダの一番新しい取り込み結果を使います: {args[0].name}")

    failed, ok_rows = {}, []
    for p in args:
        f, ok = read_result(p)
        failed.update(f)
        ok_rows.extend(ok)
        print(f"{p.name}: 成功 {len(ok)}行 / 失敗 {len(f)}行")
    if not failed:
        print("失敗した行はありません。作り直す必要はありません。")
        return

    if not acquire_lock():
        raise SystemExit("10分ごとの処理が動いているため中止しました。1分ほど待ってからもう一度実行してください")
    try:
        pending = sorted(TODO_DIR.glob(UPLOAD_GLOB))
        moved = []
        BEFORE_DIR.mkdir(parents=True, exist_ok=True)
        for p in pending:
            dest = BEFORE_DIR / p.name
            try:
                p.replace(dest)
            except OSError:
                for back in moved:  # 途中で失敗したら元に戻す
                    back.replace(TODO_DIR / back.name)
                raise SystemExit(f"{p.name} を開いているため動かせません。Excel などで閉じてから、もう一度実行してください")
            moved.append(dest)

        lines, seen, removed = [], set(), 0
        for p in moved:
            for r in read_csv(p)[1:]:
                if len(r) < 5 or not r[1].strip():
                    continue
                if r[1] in failed:
                    removed += 1
                    continue
                if r[1] not in seen:
                    seen.add(r[1])
                    lines.append(r[:5])
        added = 0
        for r in ok_rows:  # 取り込み結果の成功行（元ファイルを Uploaded に移していても失われないように）
            if r[1] not in failed and r[1] not in seen:
                seen.add(r[1])
                lines.append(r)
                added += 1

        now = datetime.now()
        if lines:
            out = TODO_DIR / f"メルカリ_在庫更新_アップロード_エラー除外_{now:%Y%m%d_%H%M%S}.csv"
            with open(out, "w", newline="", encoding=ENC, errors="replace") as f:
                w = csv.writer(f, lineterminator="\r\n")
                w.writerow(HEADER)
                w.writerows(lines)
        REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
        err = REFERENCE_DIR / f"メルカリ_エラー行_{now:%Y%m%d_%H%M%S}.csv"
        with open(err, "w", newline="", encoding=ENC, errors="replace") as f:
            w = csv.writer(f, lineterminator="\r\n")
            w.writerow(["商品管理コード", "商品ID", "SKU1_ID", "メルカリの現在の在庫数", "エラー理由"])
            for sku_id, (code, pid, cur, reason) in failed.items():
                w.writerow([code, pid, sku_id, cur, reason])
    finally:
        LOCK_FILE.unlink(missing_ok=True)

    print()
    print(f"todo に残っていた在庫更新ファイル {len(moved)}つ → todo\\エラー除外前\\ に移しました")
    print(f"失敗した行を除外: {removed}行（失敗は全部で {len(failed)}件。一覧: todo\\参考\\{err.name}）")
    if lines:
        print(f"作り直したファイル: todo\\{out.name}（{len(lines)}行。うち取り込み結果の成功行から追加 {added}行）")
        print("このファイルをメルカリの「一括編集」からアップロードしてください。")
    else:
        print("アップロードが必要な行は残っていません。")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        if str(e):
            print(f"エラー: {e}")
