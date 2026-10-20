import logging
import csv
import gc
from pathlib import Path
from datetime import datetime
import os
import configparser
import subprocess
import tempfile

# Windows と Linux の両方に対応
if os.name == 'nt':  # Windows
    OUTPUT_FOLDER = Path(r"C:\Users\nito\OneDrive\ドキュメント\RakutenSkuDelete")
else:  # Linux
    OUTPUT_FOLDER = Path("/sessions/zealous-friendly-ptolemy/mnt/RakutenSkuDelete")

DL_FOLDER = OUTPUT_FOLDER / "dl"
DELETED_HISTORY_FILE = OUTPUT_FOLDER / "deleted_manage_numbers.txt"

# ログ設定
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
log_file = OUTPUT_FOLDER / f"batch_auto_log_{timestamp}.log"
csv_folder = OUTPUT_FOLDER / f"batch_delete_csv_{timestamp}"
csv_folder.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_file, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ========== SFTP 設定を読み込む ==========
def load_sftp_config():
    """設定ファイルから SFTP 設定を読み込む"""
    config_file = OUTPUT_FOLDER / "sftp_config.ini"

    if not config_file.exists():
        logger.error(f"❌ 設定ファイルが見つかりません: {config_file}")
        logger.error(f"   以下のファイルを作成してパスワードを設定してください:")
        logger.error(f"   {config_file}")
        return None

    config = configparser.ConfigParser()
    config.read(config_file, encoding='utf-8')

    try:
        sftp_config = {
            "host": config.get("SFTP", "host"),
            "port": config.getint("SFTP", "port"),
            "username": config.get("SFTP", "username"),
            "password": config.get("SFTP", "password")
        }

        # パスワードが未設定か確認
        if "ここに" in sftp_config["password"] or sftp_config["password"].strip() == "":
            logger.error("❌ パスワードが設定されていません")
            logger.error(f"   {config_file} を編集してパスワードを入力してください")
            return None

        return sftp_config

    except Exception as e:
        logger.error(f"❌ 設定ファイルの読み込みエラー: {e}")
        return None

SFTP_REMOTE_PATH = "ritem/batch"

# ========== 削除済みリストを読み込む ==========
def load_deleted_history():
    """削除済み管理番号の履歴を読み込む"""
    deleted_set = set()

    if DELETED_HISTORY_FILE.exists():
        try:
            with open(DELETED_HISTORY_FILE, 'r', encoding='utf-8') as f:
                deleted_set = set(line.strip() for line in f if line.strip())
            logger.info(f"✅ 削除済み管理番号を読み込み: {len(deleted_set):,}件")
        except Exception as e:
            logger.warning(f"⚠️ 削除履歴の読み込み失敗: {e}")

    return deleted_set

# ========== 削除済みリストに追加 ==========
def save_deleted_history(manage_numbers):
    """新規削除管理番号を履歴ファイルに追加"""
    try:
        with open(DELETED_HISTORY_FILE, 'a', encoding='utf-8') as f:
            for manage_number in manage_numbers:
                f.write(f"{manage_number}\n")
        logger.info(f"✅ 削除済みリストを更新: {len(manage_numbers):,}件を追加")
    except Exception as e:
        logger.error(f"❌ 削除履歴の保存失敗: {e}")

# ========== ステップ1: ローカル CSV ファイルを取得 ==========
def get_local_csv_files():
    """ローカルの dl/ フォルダから CSV ファイルを取得"""
    logger.info("\n" + "="*60)
    logger.info("ステップ1: ローカル CSV ファイル一覧を取得")
    logger.info("="*60)

    if not DL_FOLDER.exists():
        logger.error(f"❌ フォルダが見つかりません: {DL_FOLDER}")
        return []

    csv_files = list(DL_FOLDER.glob("*.csv"))
    csv_files.sort()

    if not csv_files:
        logger.error(f"❌ CSV ファイルが見つかりません: {DL_FOLDER}")
        return []

    logger.info(f"✅ CSV ファイル一覧:")
    for f in csv_files:
        logger.info(f"  - {f.name} ({f.stat().st_size:,} bytes)")

    logger.info(f"✅ 合計 {len(csv_files)} ファイル\n")

    return csv_files

# ========== ステップ2: CSV を読み込み & 統合 ==========
def read_and_merge_csv(csv_files):
    """複数の CSV を読み込んで統合"""
    logger.info("="*60)
    logger.info("ステップ2: CSV を読み込み & 統合")
    logger.info("="*60)

    all_rows = []

    for idx, csv_file in enumerate(csv_files, 1):
        try:
            logger.info(f"読み込み中 ({idx}/{len(csv_files)}): {csv_file.name}...")

            with open(csv_file, 'r', encoding='shift_jis', errors='ignore') as f:
                reader = csv.DictReader(f)
                rows = list(reader)
                all_rows.extend(rows)

            logger.info(f"  ✅ {len(rows):,}件を読み込み（累計: {len(all_rows):,}件）")

        except Exception as e:
            logger.error(f"  ❌ 読み込みエラー: {e}")

        if idx % 3 == 0:
            gc.collect()

    logger.info(f"\n✅ 統合完了: {len(all_rows):,}件\n")

    return all_rows

# ========== ステップ3: 管理番号を抽出 & 重複排除（削除済みを除外） ==========
def extract_manage_numbers(rows, deleted_set):
    """CSV から商品管理番号を抽出 & 重複排除 & 削除済みを除外"""
    logger.info("="*60)
    logger.info("ステップ3: 商品管理番号を抽出（削除済みを除外）")
    logger.info("="*60)

    manage_numbers_set = set()  # 重複排除用
    manage_numbers = []

    # 列名の候補
    target_columns = ['商品管理番号（商品URL）', '商品管理番号', 'manageNumber']

    for idx, row in enumerate(rows):
        manage_number = None

        # 指定された列名でチェック
        for col_name in target_columns:
            if col_name in row:
                manage_number = row[col_name]
                break

        if manage_number:
            manage_number = manage_number.strip()
            # 重複チェック（2行ごとの重複を排除）
            # 削除済みではないかチェック
            if manage_number and manage_number not in manage_numbers_set and manage_number not in deleted_set:
                manage_numbers_set.add(manage_number)
                manage_numbers.append(manage_number)

        if (idx + 1) % 100000 == 0:
            logger.info(f"処理中: {idx + 1:,}件 (ユニーク: {len(manage_numbers_set):,}件)")
            gc.collect()

    logger.info(f"✅ 抽出完了: {len(manage_numbers):,}件（重複・削除済み除外済み）\n")

    if len(manage_numbers) == 0:
        logger.warning(f"⚠️ 新規削除対象がありません（すべて削除済みまたは重複）")

    return manage_numbers

# ========== ステップ4: 削除ファイルを生成 ==========
def generate_delete_files(manage_numbers):
    """50,000件ごとに削除ファイルを生成"""
    if not manage_numbers:
        return []

    logger.info("="*60)
    logger.info("ステップ4: 削除ファイルを生成（50,000件ごと）")
    logger.info("="*60 + "\n")

    csv_files = []
    batch_size = 50000
    batch_num = 1

    for i in range(0, len(manage_numbers), batch_size):
        batch = manage_numbers[i:i+batch_size]
        start_idx = i + 1
        end_idx = min(i + batch_size, len(manage_numbers))

        csv_filename = f"item-delete_batch_{batch_num}.csv"
        csv_filepath = csv_folder / csv_filename

        try:
            # cp932（SHIFT-JIS互換、BOM対応） + CRLF 改行コード
            with open(csv_filepath, 'w', newline='', encoding='cp932') as f:
                writer = csv.writer(f, lineterminator='\r\n')
                writer.writerow(["商品管理番号（商品URL）"])
                for manage_number in batch:
                    if manage_number:
                        writer.writerow([manage_number])

            csv_files.append({
                "batch_num": batch_num,
                "filename": csv_filename,
                "filepath": csv_filepath,
                "count": len(batch),
                "range": f"{start_idx:,}～{end_idx:,}"
            })

            logger.info(f"【バッチ {batch_num}】生成完了: {csv_filename}")
            logger.info(f"  件数: {len(batch):,}件 ({start_idx:,}～{end_idx:,})")

            batch_num += 1

        except Exception as e:
            logger.error(f"❌ CSV生成エラー: {e}")

    logger.info(f"\n✅ 合計 {len(csv_files)} 個の削除ファイルを生成\n")

    return csv_files

# ========== ステップ5: WinSCP でアップロード ==========
def upload_to_sftp_winscp(csv_files, sftp_config):
    """WinSCP を使用してファイルをアップロード"""
    logger.info("="*60)
    logger.info("ステップ5: SFTP でアップロード（WinSCP 使用）")
    logger.info("="*60 + "\n")

    uploaded_files = []
    failed_files = []

    # WinSCP のインストール確認
    winscp_path = Path(r"C:\Program Files (x86)\WinSCP\WinSCP.com")
    if not winscp_path.exists():
        logger.error(f"❌ WinSCP が見つかりません: {winscp_path}")
        logger.error("   WinSCP をインストールしてください: https://winscp.net/eng/download.php")
        return uploaded_files, failed_files

    try:
        # WinSCP スクリプトを生成
        script_lines = [
            f"open sftp://{sftp_config['username']}:{sftp_config['password']}@{sftp_config['host']}:{sftp_config['port']}",
            "option batch abort",
            "option confirm off"
        ]

        # アップロードコマンドを追加
        for csv_file in csv_files:
            local_path = str(csv_file['filepath'])
            remote_filename = csv_file['filename']
            script_lines.append(f'put "{local_path}" "{SFTP_REMOTE_PATH}/{remote_filename}"')

        script_lines.append("exit")

        # スクリプトファイルを一時作成
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8') as f:
            script_file = Path(f.name)
            f.write('\n'.join(script_lines))

        logger.info(f"🔗 WinSCP で接続中: {sftp_config['host']}:{sftp_config['port']}...")

        # WinSCP を実行
        result = subprocess.run(
            [str(winscp_path), '/script=' + str(script_file)],
            capture_output=True,
            text=True,
            timeout=300
        )

        # スクリプトファイルを削除
        script_file.unlink()

        if result.returncode == 0:
            logger.info(f"✅ WinSCP アップロード成功\n")

            # すべてのファイルが成功したと見なす
            for csv_file in csv_files:
                file_size = csv_file['filepath'].stat().st_size
                logger.info(f"✅ {csv_file['filename']} ({file_size:,} bytes)")

                uploaded_files.append({
                    "filename": csv_file['filename'],
                    "size": file_size,
                    "status": "✅ 成功"
                })
        else:
            logger.error(f"❌ WinSCP アップロード失敗")
            logger.error(f"   stdout: {result.stdout}")
            logger.error(f"   stderr: {result.stderr}")

            # すべてのファイルが失敗
            for csv_file in csv_files:
                failed_files.append({
                    "filename": csv_file['filename'],
                    "error": "WinSCP 実行エラー"
                })

    except subprocess.TimeoutExpired:
        logger.error(f"❌ WinSCP タイムアウト（300秒以上）")
        for csv_file in csv_files:
            failed_files.append({
                "filename": csv_file['filename'],
                "error": "タイムアウト"
            })
    except Exception as e:
        logger.error(f"❌ エラー: {e}")
        for csv_file in csv_files:
            failed_files.append({
                "filename": csv_file['filename'],
                "error": str(e)
            })

    return uploaded_files, failed_files

# ========== メイン実行 ==========
logger.info("\n" + "="*60)
logger.info("Rakuten SKU削除ツール v9（削除履歴管理版）")
logger.info("="*60)

start_time = datetime.now()
manage_numbers = []
csv_files = []
uploaded_files = []
failed_files = []
deleted_set = set()

try:
    # 削除済み履歴を読み込む
    deleted_set = load_deleted_history()
    logger.info("")

    # 設定ファイルを読み込む
    sftp_config = load_sftp_config()

    if not sftp_config:
        logger.error("❌ SFTP 設定の読み込み失敗")
        exit(1)

    # ステップ1: ローカル CSV ファイル取得
    local_csv_files = get_local_csv_files()

    if not local_csv_files:
        logger.error("❌ CSV ファイルが見つかりません")
        exit(1)

    # ステップ2: 読み込み & 統合
    rows = read_and_merge_csv(local_csv_files)

    if not rows:
        logger.error("❌ CSV 読み込み失敗")
        exit(1)

    # ステップ3: 管理番号抽出（削除済みを除外）
    manage_numbers = extract_manage_numbers(rows, deleted_set)

    if not manage_numbers:
        logger.warning("⚠️ 新規削除対象がありません")
        logger.info("   すべてのファイルが削除済みまたは重複です")
    else:
        # ステップ4: 削除ファイル生成
        csv_files = generate_delete_files(manage_numbers)

        if not csv_files:
            logger.error("❌ 削除ファイル生成失敗")
            exit(1)

        # ステップ5: SFTP アップロード
        uploaded_files, failed_files = upload_to_sftp_winscp(csv_files, sftp_config)

        # 削除済み履歴に追加
        if uploaded_files:
            save_deleted_history(manage_numbers)

    logger.info("="*60)
    logger.info("✅ 処理完了")
    logger.info("="*60)

except KeyboardInterrupt:
    logger.info("\n❌ ユーザーが中断しました")
except Exception as e:
    logger.error(f"❌ 予期しないエラー: {e}")
    import traceback
    logger.error(traceback.format_exc())

finally:
    end_time = datetime.now()
    elapsed = (end_time - start_time).total_seconds()
    elapsed_time = f"{int(elapsed//3600)}時間 {int((elapsed%3600)//60)}分 {int(elapsed%60)}秒"

    logger.info("\n" + "="*60)
    logger.info("📊 処理サマリー")
    logger.info("="*60)
    logger.info(f"入力ファイル数: {len(local_csv_files) if 'local_csv_files' in locals() else 0}個")
    logger.info(f"読み込み件数: {len(rows) if 'rows' in locals() else 0:,}件")
    logger.info(f"削除済み除外: {len(deleted_set):,}件")
    logger.info(f"新規削除対象: {len(manage_numbers):,}件")
    logger.info(f"生成ファイル数: {len(csv_files)}個")
    logger.info(f"アップロード成功: {len(uploaded_files)}/{len(csv_files)}個")
    logger.info(f"累計削除対象: {len(deleted_set) + len(manage_numbers):,}件")

    if failed_files:
        logger.info(f"\n❌ アップロード失敗:")
        for failed in failed_files:
            logger.info(f"  - {failed['filename']}: {failed['error']}")
    else:
        if len(csv_files) > 0:
            logger.info(f"✅ すべてのファイルがアップロード完了")
        else:
            logger.info(f"ℹ️ 新規削除対象なし")

    logger.info(f"\n所要時間: {elapsed_time}")
    logger.info(f"CSVフォルダ: {csv_folder}")
    logger.info(f"ログ: {log_file}")
    logger.info(f"削除履歴: {DELETED_HISTORY_FILE} ({len(deleted_set) + len(manage_numbers):,}件)")
    logger.info("="*60 + "\n")

    print(f"\n✅ 処理完了！")
    if len(csv_files) > 0:
        print(f"新規アップロード: {len(uploaded_files)}/{len(csv_files)}個")
    else:
        print(f"新規削除対象: なし")
    print(f"累計削除対象: {len(deleted_set) + len(manage_numbers):,}件")
    print(f"ログ: {log_file}")
