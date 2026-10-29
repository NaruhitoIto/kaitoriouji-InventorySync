# CLAUDE.md（wasabi-dashboard-sync）

WASABI SWITCH の売上集計を GitHub Actions で取得し、`data/latest_sales.json` に保存するリポジトリです。claude.ai のクラウドのルーティンがこの JSON を読み、売上ダッシュボード（Artifact）に書き込みます。
このファイルは、どのPCの Claude Code でも同じ前提で作業できるように GitHub に置いています。

## 進め方
- 返答は日本語で書く。
- `data/latest_sales.json` は Actions（github-actions[bot]）が毎時17分にコミットする。作業前に必ず `git pull`、push の前にも `git pull --rebase`。
- このリポジトリは公開。WASABI と楽天のキーは Actions の secret（`WASABI_AUTH`、`RAKUTEN_SERVICE_SECRET`、`RAKUTEN_LICENSE_KEY`、`YAHOO_CLIENT_ID`、`YAHOO_CLIENT_SECRET`、`YAHOO_REFRESH_TOKEN`、`YAHOO_SELLER_ID`）にだけ置き、ファイル・ログ・コミットに書かない。
- WASABI の `/api/shops` は呼ばない（接続モールの認証情報が返るため）。
- 今すぐ取得し直す：`gh workflow run sync.yml -R NaruhitoIto/wasabi-dashboard-sync`（過去分もまとめて取るときは `-f window_days=19` など）。
