# -*- coding: utf-8 -*-
"""
楽天RMSの注文APIから、前回以降の注文を取得し、
その商品（SKU）の在庫を 0 にするファイルをワサビスイッチのFTP（/stock_import）へ送る。

使い方:
  python rakuten_orders_sync.py --dry-run   # ファイルは作るが、FTPへは送らない（まず必ずこれで確認）
  python rakuten_orders_sync.py             # 送信する（FTP）
"""
import sys, json, base64, configparser, ftplib, io, logging, time
from pathlib import Path
from datetime import datetime, timedelta, timezone
import requests

BASE_DIR = Path(__file__).resolve().parent
RAKUTEN_CFG = BASE_DIR / "rakuten_api_config.ini"
WASABI_FTP_CFG = BASE_DIR / "wasabi_ftp_config.ini"
STATE_FILE = BASE_DIR / "rakuten_orders_state.json"   # 処理済み注文番号と最終取得時刻
OUT_DIR = BASE_DIR / "stock_csv"
LOG_DIR = BASE_DIR / "logs"
JST = timezone(timedelta(hours=9))

RMS_SEARCH_URL = "https://api.rms.rakuten.co.jp/es/2.0/order/searchOrder/"
# 注文の進捗コードのうち、キャンセル扱いにする値（在庫を戻すので減算しない）
CANCEL_PROGRESS = {"700", "800", "900"}
API_VERSION = 6  # RMS注文API のバージョン（エラーが出る場合はここを変更）

LOG_DIR.mkdir(exist_ok=True)
OUT_DIR.mkdir(exist_ok=True)
log_file = LOG_DIR / f"rakuten_orders_{datetime.now(JST):%Y%m%d_%H%M%S}.log"
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s",
                    handlers=[logging.FileHandler(log_file, encoding="utf-8"), logging.StreamHandler(sys.stdout)])
log = logging.getLogger("rakuten_orders_sync")


def load_configs():
    r = configparser.ConfigParser(); r.read(RAKUTEN_CFG, encoding="utf-8")
    w = configparser.ConfigParser(); w.read(WASABI_FTP_CFG, encoding="utf-8")
    rk, wf = r["RAKUTEN_API"], w["WASABI_FTP"]
    for k in ("service_secret", "license_key"):
        if not rk.get(k, "").strip():
            raise SystemExit(f"rakuten_api_config.ini の {k} が空です")
    return rk, wf


def load_state(initial_last_checked):
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"last_checked": initial_last_checked, "processed": []}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def rms_headers(rk):
    token = base64.b64encode(f"{rk['service_secret'].strip()}:{rk['license_key'].strip()}".encode()).decode()
    return {"Authorization": f"ESA {token}", "Content-Type": "application/json; charset=UTF-8"}


def search_orders(rk, start, end):
    """期間内の注文を全ページ取得して返す（注文番号・進捗・商品リストを含む）。"""
    orders, page = [], 1
    while True:
        body = {
            "version": API_VERSION,
            "dateType": 1,  # 1 = 注文日（RMS仕様で要確認）
            "startDatetime": start.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "endDatetime": end.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "PaginationRequestModel": {"requestRecordsAmount": 100, "requestPage": page},
        }
        r = requests.post(RMS_SEARCH_URL, headers=rms_headers(rk), json=body, timeout=60)
        if r.status_code != 200:
            raise SystemExit(f"RMS API エラー HTTP {r.status_code}: {r.text[:500]}")
        data = r.json()
        if page == 1:
            log.info(f"APIレスポンスのキー: {list(data.keys())}")
        if data.get("MessageModelList") and any(m.get("messageType") == "ERROR" for m in data["MessageModelList"]):
            raise SystemExit(f"RMS API エラー: {data['MessageModelList']}")
        # 注文番号リストを取得し、詳細（商品）を getOrder で読む
        numbers = data.get("orderNumberList") or []
        if numbers:
            orders.extend(get_order_details(rk, numbers))
        pag = (data.get("PaginationResponseModel") or {})
        total_pages = int(pag.get("totalPages", 1) or 1)
        if page >= total_pages or not numbers:
            break
        page += 1
    return orders


def get_order_details(rk, numbers):
    url = "https://api.rms.rakuten.co.jp/es/2.0/order/getOrder/"
    r = requests.post(url, headers=rms_headers(rk), json={"orderNumberList": numbers, "version": API_VERSION}, timeout=60)
    if r.status_code != 200:
        raise SystemExit(f"RMS getOrder エラー HTTP {r.status_code}: {r.text[:500]}")
    return r.json().get("OrderModelList", [])


def collect_skus(orders, processed):
    """未処理・非キャンセルの注文から、{SKU(大文字): 注文番号リスト} を作る。"""
    skus, used_orders = {}, []
    for o in orders:
        num = o.get("orderNumber")
        if not num or num in processed:
            continue
        if str(o.get("orderProgress", "")) in CANCEL_PROGRESS:
            continue
        for pkg in o.get("PackageModelList", []):
            for it in pkg.get("ItemModelList", []):
                mg = (it.get("manageNumber") or "").strip()
                if mg:
                    skus.setdefault(mg.upper(), set()).add(num)
        used_orders.append(num)
    return skus, used_orders


def write_stock_csv(skus, newline):
    stamp = datetime.now(JST).strftime("%Y%m%d_%H%M%S")
    name = f"wasabi_stock_{stamp}.csv"
    path = OUT_DIR / name
    lines = ["コントロールカラム,商品番号,在庫数"] + [f"u,{s},0" for s in sorted(skus)]
    eol = "\r\n" if newline.upper() == "CRLF" else "\n"
    text = eol.join(lines) + eol
    path.write_bytes(text.encode("cp932", errors="replace"))
    return path


def upload_ftp(wf, path):
    """一時名でアップロードしてから正式名にリネームする（取込の途中読みを防ぐ）。"""
    tmp = path.name + ".tmp"
    with ftplib.FTP() as ftp:
        ftp.connect(wf["host"], int(wf.get("port", "21") or 21), timeout=60)
        ftp.login(wf["username"], wf["password"])
        ftp.cwd(wf["remote_dir"])
        with open(path, "rb") as f:
            ftp.storbinary(f"STOR {tmp}", f)
        ftp.rename(tmp, path.name)
    log.info(f"ワサビFTPへ送信: {wf['remote_dir']}/{path.name}")


def main():
    dry = "--dry-run" in sys.argv
    rk, wf = load_configs()
    now = datetime.now(JST)
    state = load_state(rk["last_checked"])
    start = datetime.fromisoformat(state["last_checked"])
    log.info(f"モード: {'dry-run（FTP送信なし）' if dry else '送信'} / 取得期間: {start} 〜 {now}")

    orders = search_orders(rk, start, now)
    log.info(f"取得した注文: {len(orders)}件")
    skus, used = collect_skus(orders, set(state["processed"]))
    log.info(f"在庫0にする対象SKU: {len(skus)}件 / 対象注文: {len(used)}件")
    if not skus:
        state["last_checked"] = now.isoformat()
        if not dry:
            save_state(state)
        log.info("対象がないので終了します")
        return

    path = write_stock_csv(skus, wf.get("newline", "LF"))
    log.info(f"在庫ファイルを作成: {path.name}（{len(skus)}件）")
    if dry:
        log.info("dry-run のため、FTPへは送っていません。ファイルの中身を確認してください。")
        return

    upload_ftp(wf, path)  # 失敗時は例外で止まり、状態は更新しない
    state["processed"].extend(used)
    state["last_checked"] = now.isoformat()
    save_state(state)
    log.info("送信と状態の更新が完了しました")


if __name__ == "__main__":
    main()
