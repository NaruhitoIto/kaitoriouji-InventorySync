# -*- coding: utf-8 -*-
"""
Yahoo!ショッピングAPIのアクセストークンを取得する（Yahoo! ID連携・認可コードフロー）。

使い方:
  1. yahoo_auth.bat をダブルクリックする（ブラウザでログイン画面が開く）
     テスト環境の設定（[YAHOO_SHOPPING_TEST]）でログインするときは python yahoo_auth.py test
  2. Yahoo! JAPAN ID でログインし、「同意」を押す
  3. コールバックURL（ストアのページ）が開いたら、アドレス欄のURLを全部コピーし、黒い画面に貼り付けて Enter
  → yahoo_shopping_token_<environment>.json に保存される（.gitignore 済み。チャットやGitHubに貼らないこと）

キーは yahoo_shopping_config.ini（yahoo_shopping_config.ini.sample をコピーして作る）から読む。
"""
import secrets
import sys
import webbrowser
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from yahoo_shopping_api import TOKEN_URL, load_config, save_token

AUTH_URL = "https://auth.login.yahoo.co.jp/yconnect/v2/authorization"


def main():
    cfg = load_config("test" if "test" in sys.argv[1:] else None)
    print(f"環境: {cfg['environment']} / ストアアカウント: {cfg['seller_id']}")
    state, nonce = secrets.token_urlsafe(16), secrets.token_urlsafe(16)
    url = AUTH_URL + "?" + urlencode({"response_type": "code", "client_id": cfg["client_id"], "redirect_uri": cfg["redirect_uri"],
                                      "scope": "openid profile", "state": state, "nonce": nonce})
    print("ブラウザでログイン画面を開きます。Yahoo! JAPAN ID でログインし、「同意」を押してください。")
    print("（ブラウザが開かないときは、次のURLをブラウザに貼り付けてください）")
    print(url)
    print()
    webbrowser.open(url)
    print("同意するとストアのページが開きます。そのページのアドレス欄のURLを全部コピーし、")
    pasted = input("ここに貼り付けて Enter を押してください: ").strip()
    q = parse_qs(urlparse(pasted).query)
    if "error" in q:
        raise SystemExit(f"ログイン・同意がされませんでした: {q.get('error')} {q.get('error_description', [''])[0]}")
    code = (q.get("code") or [""])[0]
    if not code:
        raise SystemExit("貼り付けたURLに code= がありません。同意した後に開いたページのURLを貼り付けてください")
    if (q.get("state") or [""])[0] != state:
        raise SystemExit("URL の state が一致しません。もう一度最初からやり直してください")
    r = requests.post(TOKEN_URL, timeout=60, auth=(cfg["client_id"], cfg["client_secret"]),
                      data={"grant_type": "authorization_code", "code": code, "redirect_uri": cfg["redirect_uri"]})
    if r.status_code != 200:
        try:
            err = r.json()
        except ValueError:
            err = {"error": r.text[:200]}
        raise SystemExit(f"トークンの取得に失敗しました（HTTP {r.status_code}）: {err.get('error')} {err.get('error_description', '')}")
    tok = r.json()
    save_token(cfg, tok)
    print()
    print(f"トークンを保存しました: {cfg['token_file'].name}（アクセストークンの有効期限 {int(tok.get('expires_in', 0)) // 60}分。"
          f"以後はリフレッシュトークンで自動更新します）")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        if str(e):
            print(f"エラー: {e}")
