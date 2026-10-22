# -*- coding: utf-8 -*-
"""
Yahoo!ショッピングAPIのアクセストークンを取得する（Yahoo! ID連携・認可コードフロー）。

使い方:
  1. yahoo_auth.bat をダブルクリックする（ブラウザでログイン画面が開く）
  2. Yahoo! JAPAN ID でログインし、「同意」を押す
  3. コールバックURL（ストアのページ）が開いたら、アドレス欄のURLを全部コピーし、黒い画面に貼り付けて Enter
  → yahoo_shopping_token.json に保存される（.gitignore 済み。チャットやGitHubに貼らないこと）

キーは yahoo_shopping_config.ini（yahoo_shopping_config.ini.sample をコピーして作る）から読む。
"""
import configparser
import json
import secrets
import time
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "yahoo_shopping_config.ini"
TOKEN_FILE = BASE_DIR / "yahoo_shopping_token.json"
AUTH_URL = "https://auth.login.yahoo.co.jp/yconnect/v2/authorization"
TOKEN_URL = "https://auth.login.yahoo.co.jp/yconnect/v2/token"


def load_config():
    if not CONFIG_FILE.exists():
        raise SystemExit(f"設定ファイルがありません: {CONFIG_FILE}（yahoo_shopping_config.ini.sample をコピーして作成してください）")
    c = configparser.ConfigParser()
    c.read(CONFIG_FILE, encoding="utf-8")
    s = c["YAHOO_SHOPPING"]
    cfg = {k: s.get(k, "").strip() for k in ("client_id", "client_secret", "redirect_uri", "seller_id", "environment")}
    for k in ("client_id", "client_secret", "redirect_uri"):
        if not cfg[k] or "ここに" in cfg[k]:
            raise SystemExit(f"yahoo_shopping_config.ini の {k} が未入力です")
    return cfg


def save_token(tok):
    tok["obtained_at"] = int(time.time())
    TOKEN_FILE.write_text(json.dumps(tok, ensure_ascii=False, indent=1), encoding="utf-8")


def refresh(cfg):
    """保存済みのリフレッシュトークンでアクセストークンを更新する。成功なら新しいトークン情報を返す。"""
    old = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    r = requests.post(TOKEN_URL, timeout=60, auth=(cfg["client_id"], cfg["client_secret"]),
                      data={"grant_type": "refresh_token", "refresh_token": old["refresh_token"]})
    if r.status_code != 200:
        raise RuntimeError(f"トークンの更新に失敗しました（HTTP {r.status_code}）。yahoo_auth.bat でログインし直してください")
    tok = r.json()
    tok.setdefault("refresh_token", old["refresh_token"])
    save_token(tok)
    return tok


def main():
    cfg = load_config()
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
    save_token(tok)
    print()
    print(f"トークンを保存しました: {TOKEN_FILE.name}（アクセストークンの有効期限 {int(tok.get('expires_in', 0)) // 60}分。"
          f"リフレッシュトークンで更新できます）")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        if str(e):
            print(f"エラー: {e}")
