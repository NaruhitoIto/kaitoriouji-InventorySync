# CLAUDE.md（wasabi-dashboard-sync）

WASABI SWITCH の売上集計を GitHub Actions で取得し、`data/latest_sales.json` に保存するリポジトリです。claude.ai のクラウドのルーティンがこの JSON を読み、売上ダッシュボード（Artifact）に書き込みます。
このファイルは、どのPCの Claude Code でも同じ前提で作業できるように GitHub に置いています。

## 進め方
- 返答は日本語で書く。
- `data/latest_sales.json` は Actions（github-actions[bot]）が毎時17分にコミットする。作業前に必ず `git pull`、push の前にも `git pull --rebase`。
- このリポジトリは公開。WASABI と楽天のキーは Actions の secret（`WASABI_AUTH`、`RAKUTEN_SERVICE_SECRET`、`RAKUTEN_LICENSE_KEY`）にだけ置き、ファイル・ログ・コミットに書かない。ヤフーのキーは下記の固定IPのPCのユーザー環境変数にだけ置く。
- WASABI の `/api/shops` は呼ばない（接続モールの認証情報が返るため）。
- 今すぐ取得し直す：`gh workflow run sync.yml -R NaruhitoIto/wasabi-dashboard-sync`（過去分もまとめて取るときは `-f window_days=19` など）。

## ヤフーショッピング（店舗8）
- 注文APIは申請した固定IPのPCからしか呼べないため、Actions では呼ばない。そのPCのタスクスケジューラ（`wasabi-yahoo-sync`、毎時05分）が、専用クローン `%LOCALAPPDATA%\wasabi-dashboard-sync\repo` で `scripts/yahoo_sync.py` を実行し、集計値だけを `data/yahoo_sales.json` に書いてプッシュする。Actions の `fetch_sales.py` はそれを `scripts/yahoo_file.py` で読み、`YAHOO_FROM` 以降の日を店舗8に反映する。
- 認証情報（`YAHOO_CLIENT_ID`、`YAHOO_CLIENT_SECRET`、`YAHOO_REFRESH_TOKEN`、`YAHOO_SELLER_ID`、`YAHOO_REDIRECT_URI`、`YAHOO_PUBLIC_KEY_PATH`、`YAHOO_PUBLIC_KEY_VERSION`）はそのPCのユーザー環境変数。公開鍵ファイルはリポジトリの外に置く。
- 公開鍵認証を使っているので再認可は4週間ごと。期限は `data/yahoo_sales.json` の `auth_expires`。そのPCで `python scripts/yahoo_auth.py` を実行してブラウザでログインし直す（期限前なら同じトークンのまま期限が延びる）。公開鍵は1年ごとに更新。
- 金額はモールクーポン・ポイント値引き前（`TotalPrice + TotalMallCouponDiscount + UsePoint`、ストアクリエイターProの注文CSVの Sale Total に当たる。楽天の totalPrice と同じ考え方）。
- ログ：`%LOCALAPPDATA%\wasabi-dashboard-sync\yahoo_sync.log`。
