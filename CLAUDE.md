# CLAUDE.md（kaitoriouji-InventorySync）

各モール（楽天・Amazon・メルカリ・Yahoo!ショッピング・ヤフオクなど）と WASABI SWITCH の在庫・注文をそろえるスクリプト集と、売上ダッシュボード用の売上集計（`dashboard/`）です。
このファイルは、どのPCの Claude Code でも同じ前提で作業できるように GitHub に置いています。

## 進め方
- 返答・コミットメッセージは日本語で書く。
- 作業を始める前に `git pull`、終わったらコミットして `git push`（main に直接）。もう1台のPCで更新されていることがある。
- 売上集計の Actions（github-actions[bot]）が毎時17分に、固定IPのPCのタスクが毎時05分に main へコミットする。push が断られたら `git pull --rebase` してから push し直す。
- このリポジトリは公開。設定ファイル（`*_config.ini`）・キー・トークンはコミットしない。新しい設定ファイルを作ったら `.gitignore` にも足す。
- 状態ファイル（`*_state.json`、`deleted_manage_numbers.txt` など）は Git に入らない。別のPCへは手動でコピーする。
- 自動実行（`run_*.bat`）は1台のPCだけで動かす。どのPCで動いているか確かめてから実行する。
- 楽天SKU削除の毎月の手順は `手順書_毎月実行ガイド.md`。

## API の共通部品（リポジトリ直下）
- 新しく API を呼ぶときは、次の部品を使う（同じ呼び出しを各スクリプトに書かない）。
  - `wasabi_api.py`：WASABI（`wasabi_config.ini`、Actions では secret `WASABI_AUTH`）。`/api/shops` は呼ばない（接続モールの認証情報が返るため）。
  - `rakuten_rms_api.py`：楽天RMS 受注API（`rakuten_api_config.ini`、Actions では secret `RAKUTEN_SERVICE_SECRET`・`RAKUTEN_LICENSE_KEY`）。
  - `yahoo_shopping_api.py`：Yahoo!ショッピング ストアAPI（在庫API・注文API。`yahoo_shopping_config.ini` とトークン）。
  - `amazon_sp_api.py`：Amazon SP-API。`mercari_shops_api.py`：メルカリShops API。
- 出荷データ作成アプリ（`shipping_export/`）は exe にするので、共通部品は標準ライブラリと requests だけで書く（`build_exe.bat` が `--paths ..` で取り込む）。

## 在庫更新のルール
- 在庫は0か1の一点物で、更新は常に「在庫数=0」「在庫数=1」の絶対値指定。同じ値を重ねて送っても害はないので、取りこぼし防止を優先して期間は重なる側に寄せてよい。
- 新しく在庫更新を作るときも絶対値指定にし、+1／-1 の加減算にはしない。
- 古いデータで1を送ってしまう順序の問題（売れた後なのに、売れる前の在庫で1を送る）には注意する。

## ヤフーのログイン
- ストアAPIのトークンは `yahoo_auth.bat` で作る `yahoo_shopping_token_production.json`（在庫連携・出荷データ作成・売上集計の共通）。公開鍵認証を使っているので、ログインし直すのは4週間ごとに1回。期限の7日前からモール間在庫連携のログに、5日前から売上集計のログに警告が出る。公開鍵は1年ごとに更新。
- 注文APIは申請した固定IPのPC（在庫連携を動かしているPC）からしか呼べない。

## 売上ダッシュボード（`dashboard/`）
- WASABI の売上集計を GitHub Actions（`.github/workflows/dashboard_sync.yml`、毎時17分）が `dashboard/fetch_sales.py` で取得し、`dashboard/data/latest_sales.json` にコミットする。claude.ai のクラウドのルーティン（毎日9時・23時）がこの JSON を raw.githubusercontent.com から読み、売上ダッシュボード（Artifact）に書き込む。
- 楽天（店舗1）は RMS の受注API の値で差し替える（2026-10-17 から。`dashboard/rakuten_sales.py`）。
- 今すぐ取得し直す：`gh workflow run dashboard_sync.yml -R NaruhitoIto/kaitoriouji-InventorySync`（過去分もまとめて取るときは `-f window_days=19` など）。
- ヤフー（ストアAPI：ヤフーショッピング店＝店舗8、ヤフオク店＝店舗6）
  - ストアAPIの注文には、ヤフーショッピング店とヤフオク店（ストアのオークション）の両方が入る。`IsYahooAuctionOrder` で分け、オークション以外を店舗8、オークションを店舗6とする（件数のほとんどはヤフオク店）。
  - 注文APIは Actions からは呼べないため、固定IPのPCのタスクスケジューラ（`wasabi-yahoo-sync`、毎時05分）が、専用クローン `C:\InventorySync-yahoo-task`（作業には使わない）で `dashboard/yahoo_sync.py --yahoo-config C:\InventorySync\yahoo_shopping_config.ini` を実行し、集計値だけを `dashboard/data/yahoo_sales.json` に書いてプッシュする。Actions の `fetch_sales.py` はそれを `dashboard/yahoo_file.py` で読み、`YAHOO_FROM` 以降の日を店舗8に、`YAHOO_AUCTION_FROM` 以降の日を店舗6に反映する（どちらも 2026-10-17 から）。
  - 金額はモールクーポン・ポイント値引き前（`TotalPrice + TotalMallCouponDiscount + UsePoint`、ストアクリエイターProの注文CSVの Sale Total に当たる。楽天の totalPrice と同じ考え方）。
  - ログ・数量のキャッシュ：`%LOCALAPPDATA%\InventorySync-dashboard\`（`yahoo_sync.log`、`yahoo_qty_cache.json`）。
  - Claude のデスクトップアプリから `%LOCALAPPDATA%`（AppData）に作ったファイル・フォルダは、アプリ専用の場所に振り替えられてタスクスケジューラからは見えない。タスクが使うファイルを Claude が用意するときは AppData の外（`C:\` 直下など）に置く。
- 2026-10-29 に wasabi-dashboard-sync リポジトリ（アーカイブ済み）から履歴ごと移した。
