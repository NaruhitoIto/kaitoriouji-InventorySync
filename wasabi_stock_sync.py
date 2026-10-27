"""
ワサビスイッチの在庫数を楽天の在庫CSVへ反映するツール

使い方（コマンドプロンプト / PowerShell で実行）:
  python wasabi_stock_sync.py --test      接続確認（1ページだけ取得して構造を表示。ファイルは作らない）
  python wasabi_stock_sync.py --dry-run   CSVを作るだけ（アップロードしない）
  python wasabi_stock_sync.py             通常実行（CSV作成 → 確認 → SFTPアップロード）

事前に wasabi_config.ini の token と rakuten_mall_name を設定してください。
"""

import configparser
import csv
import json
import logging
import subprocess
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
WASABI_CONFIG = BASE_DIR / "wasabi_config.ini"
SFTP_CONFIG = BASE_DIR / "sftp_config.ini"
STATE_FILE = BASE_DIR / "wasabi_stock_state.json"
OUT_DIR = BASE_DIR / "stock_csv"
LOG_DIR = BASE_DIR / "logs"
WINSCP = Path(r"C:\Program Files (x86)\WinSCP\WinSCP.com")

LOG_DIR.mkdir(exist_ok=True)
OUT_DIR.mkdir(exist_ok=True)
log_file = LOG_DIR / f"stock_sync_{datetime.now():%Y%m%d_%H%M%S}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler(log_file, encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger("stock_sync")


def load_configs():
    """設定ファイルを読み込む。トークンはログに出さない。"""
    if not WASABI_CONFIG.exists():
        raise SystemExit(f"設定ファイルがありません: {WASABI_CONFIG}")
    wc = configparser.ConfigParser()
    wc.read(WASABI_CONFIG, encoding="utf-8")
    token = wc.get("WASABI", "token", fallback="").strip()
    if not token or "ここに" in token:
        raise SystemExit("wasabi_config.ini の token が未入力です（KEY:SECRET を入れてください）")

    cfg = {
        "base_url": wc.get("WASABI", "base_url", fallback="https://world-switch.com/kaitoriouji/ext"),
        "token": token,
        "rakuten_mall_name": wc.get("WASABI", "rakuten_mall_name", fallback="楽天市場店").strip(),
        "rakuten_status": wc.get("WASABI", "rakuten_status", fallback="出品中").strip(),
        "page_limit": wc.getint("WASABI", "page_limit", fallback=100),
        "remote_dir": wc.get("RAKUTEN", "remote_dir", fallback="ritem/batch"),
        # 取得した在庫1の商品の出品状況を他モールの連携（mercari_stock_sync.py など）用に保存するモール名（カンマ区切り）
        "snapshot_malls": [m.strip() for m in wc.get("WASABI", "snapshot_malls", fallback="").split(",") if m.strip()],
    }

    sc = configparser.ConfigParser()
    sc.read(SFTP_CONFIG, encoding="utf-8")
    cfg["sftp"] = {
        "host": sc.get("SFTP", "host", fallback="upload.rakuten.ne.jp"),
        "port": sc.getint("SFTP", "port", fallback=22),
        "username": sc.get("SFTP", "username", fallback=""),
        "password": sc.get("SFTP", "password", fallback=""),
    }
    return cfg


IN_STOCK_QUERY = {"在庫数": {"eq": "1"}}  # 在庫1の商品だけを取得する絞り込み


def fetch_page(cfg, page, query=None, sort="product_id"):
    """/api/products/search を1ページ取得する。429/5xx は少し待ってリトライ。"""
    url = f"{cfg['base_url'].rstrip('/')}/api/products/search"
    headers = {
        "Authorization": f"Bearer {cfg['token']}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    body = {
        "sort": sort,
        "reverse": False,
        "page": page,
        "limit": cfg["page_limit"],
        "query": query or {},
    }
    for attempt in range(4):
        try:
            r = requests.post(url, headers=headers, json=body, timeout=60)
        except requests.RequestException as e:
            log.warning(f"通信エラー（ページ {page}, 試行 {attempt + 1}）: {type(e).__name__}")
            time.sleep(3 * (attempt + 1))
            continue
        if r.status_code in (429, 500, 502, 503):
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
        return r.json()
    raise RuntimeError("リトライ上限に達しました")


def iter_products(cfg, max_pages=None, query=None):
    """該当する全商品を商品番号の昇順で返す（query に商品番号の条件は入れないこと）。
    ページ番号（オフセット）で進めると、取得中（全件で数十分）に売れて結果から消えた商品の分だけ
    後ろの商品が前にずれ、ページ境界の商品を取りこぼす。そのため「前ページ最後の商品番号以上」を
    条件に次のページを取る（product_id は絞り込みに使えないため商品番号を使う）。
    境界の商品は両方のページに出るので product_id で重複を除く。"""
    limit = cfg["page_limit"]
    seen = set()
    cursor = None
    first_total = None
    page = 0
    while True:
        page += 1
        q = dict(query or {})
        if cursor is not None:
            q["商品番号"] = {"gte": cursor}
        data = fetch_page(cfg, 1, q, sort="商品番号")
        items = data.get("data", [])
        total = data.get("total") or 0  # この位置（商品番号）以降の該当件数
        if first_total is None:
            first_total = total
        new = [it for it in items if it.get("product_id") not in seen]
        log.info(f"ページ {page}: {len(new)}件 (この位置以降 total={total})")
        for item in new:
            seen.add(item.get("product_id"))
            yield item
        if max_pages and page >= max_pages:
            break
        if len(items) < limit and total <= len(items):
            break  # 最後のページ
        if not new:
            raise RuntimeError(f"取得が先に進みません（商品番号 {cursor} 以降の該当 {total}件）")
        cursor = str(items[-1].get("商品番号") or "")
        time.sleep(0.4)  # 速度制限（約3リクエスト/秒）を守る
    log.info(f"取得完了: {len(seen):,}件（開始時の該当 {first_total:,}件）")


def listing_status(item, mall_name):
    """「出品状況」は {モール名: 状態} の辞書。該当モールの状態を返す（無ければ None）。"""
    status = item.get("出品状況")
    if not isinstance(status, dict):
        return None
    return status.get(mall_name)


def is_rakuten_listed(item, cfg):
    """楽天市場店で「出品中」の商品だけを対象にする。"""
    return listing_status(item, cfg["rakuten_mall_name"]) == cfg["rakuten_status"]


def parse_stock(raw):
    try:
        return max(int(str(raw).strip()), 0)
    except (TypeError, ValueError):
        return None


def collect_stock(cfg, max_pages=None, query=None, snapshot=None):
    """楽天の商品番号（小文字化）→ 在庫数 の辞書を作る。
    snapshot（{モール名: {}}）を渡すと、在庫1以上の商品について {商品番号: そのモールの出品状況} も集める。"""
    stock = {}
    skipped_not_listed = 0
    skipped_bad_stock = 0
    for item in iter_products(cfg, max_pages=max_pages, query=query):
        if snapshot is not None:
            record_snapshot(snapshot, item)
        if not is_rakuten_listed(item, cfg):
            skipped_not_listed += 1
            continue
        sku = str(item.get("商品番号") or "").strip()
        if not sku:
            continue
        qty = parse_stock(item.get("在庫数"))
        if qty is None:
            skipped_bad_stock += 1
            log.warning(f"在庫数が数値ではないためスキップ: {sku} ({item.get('在庫数')!r})")
            continue
        stock[sku.lower()] = qty
    log.info(
        f"対象SKU: {len(stock):,}件 / 楽天出品中以外でスキップ: {skipped_not_listed:,}件 / "
        f"在庫数不正: {skipped_bad_stock:,}件"
    )
    return stock


SNAPSHOT_FILE = BASE_DIR / "ws_instock_snapshot.json"


def record_snapshot(snapshot, item):
    sku = str(item.get("商品番号") or "").strip()
    if not sku or not parse_stock(item.get("在庫数")):
        return
    for mall, statuses in snapshot.items():
        st = listing_status(item, mall)
        if st is not None:
            statuses[sku] = st


def save_snapshot(snapshot):
    """他モールの連携用スナップショットを保存する。失敗しても楽天の処理は止めない。"""
    try:
        tmp = SNAPSHOT_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"created_at": datetime.now().isoformat(), "malls": snapshot}, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(SNAPSHOT_FILE)
        log.info("他モール用スナップショットを保存: " + " / ".join(f"{m} {len(s):,}件" for m, s in snapshot.items()))
    except Exception as e:
        log.warning(f"他モール用スナップショットを保存できませんでした: {e}")


RECHECK_BATCH = 400  # 再確認で1回に問い合わせる商品番号の数（limit より少なくし、同じ商品番号の重複に余裕を残す）


def recheck_in_stock(cfg, skus):
    """商品番号の完全一致（in・大文字小文字は区別されない）でまとめて問い合わせ、
    在庫1以上かつ楽天出品中のSKU（小文字）を返す。差分モードで「在庫0になった」と判定したSKUを、
    送信直前の最新状態で確かめ直すために使う（400件ずつ・1回2秒ほど）。"""
    found = set()
    for start in range(0, len(skus), RECHECK_BATCH):
        batch = skus[start:start + RECHECK_BATCH]
        page, got = 1, 0
        while True:
            # 在庫で絞り込まないので、取得中に売れても結果はずれない（ページ番号で進めてよい）
            data = fetch_page(cfg, page, {"商品番号": {"in": batch}})
            items = data.get("data", [])
            for item in items:
                if parse_stock(item.get("在庫数")) and is_rakuten_listed(item, cfg):
                    found.add(str(item.get("商品番号") or "").strip().lower())
            got += len(items)
            if not items or got >= (data.get("total") or 0):
                break
            page += 1
            time.sleep(0.4)
        time.sleep(0.4)
    return found


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state):
    """送信状態を保存する。OneDrive の同期などでファイルが一時的にロックされていても、数回リトライする。"""
    data = json.dumps(state, ensure_ascii=False)
    for attempt in range(6):
        try:
            STATE_FILE.write_text(data, encoding="utf-8")
            return
        except PermissionError:
            log.warning(f"送信状態ファイルを書き込めません（使用中・同期中の可能性）。再試行 {attempt + 1}/6")
            time.sleep(5)
    raise RuntimeError(
        "送信状態ファイルを保存できませんでした。OneDriveの同期を一時停止し、"
        "wasabi_stock_state.json をエディタなどで開いていないか確認してください"
    )


ZERO_QUEUE = BASE_DIR / "ws_zero_queue.txt"  # 楽天に在庫0を送ったSKU。cross_mall_sync がヤフーのストアでも0にする


def queue_zero_for_other_malls(skus):
    """楽天に在庫0を送ったSKUを ws_zero_queue.txt に追記する（cross_mall_sync が受け取り、ワサビの在庫を確かめてから
    ヤフーのストアでも0にする）。失敗しても楽天の処理は止めない。"""
    if not skus:
        return
    try:
        with open(ZERO_QUEUE, "a", encoding="utf-8") as f:
            f.write("".join(f"{s.upper()}\n" for s in skus))
    except OSError as e:
        log.warning(f"ヤフーのストア用の在庫0リスト（{ZERO_QUEUE.name}）に追記できませんでした: {e}")


CHUNK_SIZE = 5000  # 1ファイルあたりの件数


def write_inventory_csvs(changed):
    """楽天の在庫CSV（商品管理番号（商品URL）,在庫数）を cp932 / CRLF で 5,000 件ごとに作る。
    戻り値は [(ファイルパス, そのファイルに含まれる {sku: qty}), ...]"""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    items = list(changed.items())
    result = []
    for n, start in enumerate(range(0, len(items), CHUNK_SIZE), 1):
        part = dict(items[start:start + CHUNK_SIZE])
        # 楽天の取込ルール: ファイル名は normal-item で始まる必要がある
        path = OUT_DIR / f"normal-item_{stamp}_{n:02d}.csv"
        with open(path, "w", newline="", encoding="cp932", errors="replace") as f:
            w = csv.writer(f, lineterminator="\r\n")
            # RMS の normal-item 形式（ダウンロードファイルと同じ構造）
            # 商品レベル行: 商品管理番号・商品番号 / SKUレベル行: 商品管理番号・SKU管理番号・在庫数
            w.writerow(["商品管理番号（商品URL）", "商品番号", "SKU管理番号", "在庫数"])
            for sku, qty in part.items():
                w.writerow([sku, sku.upper(), "", ""])
                w.writerow([sku, "", sku, qty])
        log.info(f"在庫CSVを作成: {path.name} ({len(part):,}件)")
        result.append((path, part))
    return result


def upload_sftp(cfg, csv_path):
    """WinSCP でアップロード。成功なら True。"""
    if not WINSCP.exists():
        log.error(f"WinSCP が見つかりません: {WINSCP}")
        return False
    s = cfg["sftp"]
    remote = f"{cfg['remote_dir']}/{csv_path.name}"
    script_lines = [
        f"open sftp://{s['username']}:{s['password']}@{s['host']}:{s['port']}",
        "option batch abort",
        "option confirm off",
        f'put "{csv_path}" "{remote}"',
        "exit",
    ]
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write("\n".join(script_lines))
        script_file = Path(f.name)
    try:
        result = subprocess.run(
            [str(WINSCP), f"/script={script_file}"],
            capture_output=True, timeout=300,  # 出力はバイトのまま受け取り、後で読める形に変換する
        )
    finally:
        script_file.unlink(missing_ok=True)

    out = (result.stdout or b"").decode("cp932", errors="replace")
    err = (result.stderr or b"").decode("cp932", errors="replace")
    if result.returncode == 0:
        log.info(f"SFTPアップロード成功: {remote}")
        return True
    log.error(f"SFTPアップロード失敗 (code {result.returncode}): {out[-800:]} {err[-800:]}")
    return False


def run_test(cfg):
    """1ページだけ取得し、実データの構造（モール名・状態・SKU）を確認表示する。"""
    items = list(iter_products(cfg, max_pages=1))
    log.info(f"接続OK。取得商品数: {len(items)}件（limit={cfg['page_limit']}）")

    mall_names = Counter()
    status_values = Counter()
    types = Counter()
    for it in items:
        status = it.get("出品状況")
        types[type(status).__name__] += 1
        if isinstance(status, dict):
            for mall, st in status.items():
                mall_names[mall] += 1
                status_values[str(st)] += 1
    log.info(f"出品状況の型: {dict(types)}")
    log.info(f"モール名（出現数）: {dict(mall_names)}")
    log.info(f"状態の値（出現数）: {dict(status_values)}")
    log.info(f"設定中の判定: モール={cfg['rakuten_mall_name']} / 状態={cfg['rakuten_status']}")

    matched = [it for it in items if is_rakuten_listed(it, cfg)]
    log.info(f"判定に一致した件数（このページ内）: {len(matched)}件")
    for it in matched[:5]:
        log.info(f"  SKU例: {it.get('商品番号')} / 在庫数: {it.get('在庫数')}")


def probe_queries(cfg):
    """絞り込み条件の候補を1件ずつ試し、total（該当件数）を表示する。"""
    candidates = [
        ("在庫数 = 1", {"在庫数": {"eq": "1"}}),
        ("在庫数 = 0", {"在庫数": {"eq": "0"}}),
        ("出品状況 に 楽天市場店", {"出品状況": {"includes": "楽天市場店"}}),
        ("商品番号 が 'sk' を含む", {"商品番号": {"includes": "sk"}}),
    ]
    url = f"{cfg['base_url'].rstrip('/')}/api/products/search"
    headers = {
        "Authorization": f"Bearer {cfg['token']}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    for label, query in candidates:
        body = {"sort": "product_id", "reverse": False, "page": 1, "limit": 1, "query": query}
        try:
            r = requests.post(url, headers=headers, json=body, timeout=60)
            if r.status_code == 200:
                data = r.json()
                log.info(f"[{label}] total={data.get('total')} / 先頭: {str(data.get('data', [{}])[0].get('商品番号') if data.get('data') else '')}")
            else:
                log.info(f"[{label}] HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            log.info(f"[{label}] 失敗: {e}")
        time.sleep(0.4)


def read_csv_part(path):
    """作成済みの在庫CSVを読み直して {sku: qty} にする（ヘッダー行は除く）。"""
    part = {}
    with open(path, "r", encoding="cp932", newline="") as f:
        reader = csv.reader(f)
        next(reader, None)
        for row in reader:
            if len(row) >= 4 and row[2] != "":
                part[row[2]] = int(row[3])
    return part


def resume_existing(cfg):
    """直前の全件取得で作成済みのCSVを、normal-item 名にリネームして送信する（再取得はしない）。"""
    # 作成済みファイル（item-inventory_ または リネーム済みの normal-item_）を探す
    candidates = sorted(
        list(OUT_DIR.glob("item-inventory_*_[0-9][0-9].csv"))
        + list(OUT_DIR.glob("normal-item_*_[0-9][0-9].csv"))
    )
    if not candidates:
        raise SystemExit("再利用できる在庫CSVがありません")
    latest = candidates[-1].name
    stamp = latest.split("_", 1)[1].rsplit("_", 1)[0]  # 例: 20261017_110939
    files = sorted(
        list(OUT_DIR.glob(f"item-inventory_{stamp}_[0-9][0-9].csv"))
        + list(OUT_DIR.glob(f"normal-item_{stamp}_[0-9][0-9].csv"))
    )
    log.info(f"既存の在庫CSVを再利用します: {stamp} ({len(files)}ファイル)")

    total = len(files)
    parts = []
    for n, old in enumerate(files, 1):
        new_path = OUT_DIR / f"normal-item_{stamp}_{n:02d}.csv"
        if old != new_path and not new_path.exists():
            old.rename(new_path)
        parts.append((new_path, read_csv_part(new_path)))
    count = sum(len(p) for _, p in parts)

    answer = input(f"{count:,}件（{total}ファイル）を楽天へアップロードします。よろしいですか？ (yes と入力): ")
    if answer.strip().lower() != "yes":
        log.info("キャンセルしました。")
        return

    state = load_state()
    failed = 0
    for path, part in parts:
        if upload_sftp(cfg, path):
            state.update(part)
            save_state(state)
        else:
            failed += 1
            log.error(f"アップロード失敗: {path.name}")
    if failed:
        log.error(f"失敗ファイル: {failed}件。もう一度 --resume を実行すると、失敗分を含めて再送されます。")
    else:
        log.info("すべてのファイルのアップロードと送信状態の更新が完了しました。")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "--run"
    cfg = load_configs()
    log.info(f"モード: {mode} / 判定: {cfg['rakuten_mall_name']} = {cfg['rakuten_status']}")

    if mode == "--test":
        run_test(cfg)
        return

    if mode == "--probe":
        probe_queries(cfg)
        return

    if mode == "--resume":
        resume_existing(cfg)
        return

    state = load_state()
    auto = mode == "--auto"
    if auto and not state:
        raise SystemExit("自動実行には基準データが必要です。先に手動で一度 python wasabi_stock_sync.py を実行してください")
    full = mode == "--full" or (not state and not auto)
    snapshot = {m: {} for m in cfg["snapshot_malls"]} or None

    if full:
        # 初回（または --full）: 全商品を取得して基準を作る（在庫0も含む）
        log.info("全件取得モードで実行します（時間がかかります）")
        stock = collect_stock(cfg, snapshot=snapshot)
        changed = {sku: q for sku, q in stock.items() if state.get(sku) != q}
    else:
        # 通常: 在庫1の商品だけ取得し、前回1だったのに今回いないものを0にする
        log.info("差分モードで実行します（在庫1の商品のみ取得）")
        in_stock = collect_stock(cfg, query=IN_STOCK_QUERY, snapshot=snapshot)
        changed = {sku: 1 for sku in in_stock if state.get(sku) != 1}
        dropped = [sku for sku, q in state.items() if q == 1 and sku not in in_stock]
        if dropped:
            # 取得中のずれなどで一覧から漏れただけの商品に0を送らないよう、送信前に商品番号で確かめ直す
            log.info(f"在庫0になった候補 {len(dropped):,}件を商品番号で再確認します")
            still = recheck_in_stock(cfg, dropped)
            if still:
                log.warning(f"再確認で在庫あり・楽天出品中だったため0送信から外しました: {len(still):,}件 "
                            f"（例: {', '.join(sorted(still)[:10])}）")
                dropped = [sku for sku in dropped if sku not in still]
        for sku in dropped:
            changed[sku] = 0
        log.info(f"在庫1に戻った/新規: {len(changed) - len(dropped):,}件 / 在庫0になった: {len(dropped):,}件")
        stock = None

    if snapshot is not None:
        save_snapshot(snapshot)

    log.info(f"前回送信からの変更: {len(changed):,}件")

    if not changed:
        log.info("変更なし。アップロードは不要です。")
        return

    if auto and len(changed) > AUTO_MAX_CHANGES:
        # 想定外の大量変更（取得失敗などの可能性）は自動では送らない
        raise SystemExit(f"変更件数が多すぎるため自動送信を中止しました（{len(changed):,}件 > 上限 {AUTO_MAX_CHANGES:,}件）")

    files = write_inventory_csvs(changed)

    if mode == "--dry-run":
        log.info(f"dry-run のためアップロードしません。作成ファイル数: {len(files)}")
        return

    if not auto:
        answer = input(f"{len(changed):,}件（{len(files)}ファイル）を楽天へアップロードします。よろしいですか？ (yes と入力): ")
        if answer.strip().lower() != "yes":
            log.info("キャンセルしました。")
            return

    failed = 0
    for path, part in files:
        if upload_sftp(cfg, path):
            # ファイルごとに、アップロード成功分だけ状態を更新する（失敗分は次回また送る）
            state.update(part)
            save_state(state)
            queue_zero_for_other_malls([sku for sku, q in part.items() if q == 0])
        else:
            failed += 1
            log.error(f"アップロード失敗: {path.name}（このファイルの分は状態を更新していません）")
    if failed:
        log.error(f"失敗ファイル: {failed}件。次回実行時に差分として再送されます。")
    else:
        log.info("すべてのファイルのアップロードと送信状態の更新が完了しました。")


LOCK_FILE = BASE_DIR / "wasabi_stock_sync.lock"
AUTO_MAX_CHANGES = 20000  # 自動実行で一度に送る上限（これを超えたら中止して確認を促す）


def acquire_lock():
    """二重起動を防ぐ。古いロック（2時間以上前）は無効として扱う。"""
    if LOCK_FILE.exists():
        age = time.time() - LOCK_FILE.stat().st_mtime
        if age < 2 * 3600:
            log.warning("別の実行が進行中のため、今回は中止します")
            return False
        log.warning("古いロックファイルを無視して続行します")
    LOCK_FILE.write_text(str(datetime.now()), encoding="utf-8")
    return True


if __name__ == "__main__":
    if not acquire_lock():
        sys.exit(0)
    try:
        main()
    except SystemExit as e:
        log.error(str(e))
    except Exception as e:
        log.exception(f"予期しないエラー: {e}")
    finally:
        LOCK_FILE.unlink(missing_ok=True)
        log.info(f"ログ: {log_file}")
