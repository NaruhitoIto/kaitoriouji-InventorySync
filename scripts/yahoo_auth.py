"""Yahoo!ショッピング ストアAPIの再認可(Yahoo! JAPAN IDでのログイン)をして、
リフレッシュトークンをこのPCのユーザー環境変数 YAHOO_REFRESH_TOKEN に保存する。

- 公開鍵認証ありなら4週間ごと、なしなら12時間ごとに実行が必要(期限前に実行すれば期限が延びる)。
- 未設定のユーザー環境変数(YAHOO_CLIENT_ID / YAHOO_CLIENT_SECRET / YAHOO_SELLER_ID / YAHOO_REDIRECT_URI /
  YAHOO_PUBLIC_KEY_PATH / YAHOO_PUBLIC_KEY_VERSION)は、その場で入力を求めて保存する。
- シークレット・トークン・認可コードは画面にもファイルにも出さない。

使い方: python scripts/yahoo_auth.py
  ブラウザでログイン・同意 → 移動した先のページのURL(アドレスバー)をまるごと貼り付ける(入力は表示されない)。
"""
import ctypes
import getpass
import os
import secrets
import sys
import webbrowser
import winreg
from datetime import date, timedelta
from urllib.parse import parse_qs, urlencode, urlparse

import requests

AUTH_URL = "https://auth.login.yahoo.co.jp/yconnect/v2/authorization"
TOKEN_URL = "https://auth.login.yahoo.co.jp/yconnect/v2/token"
ENV_KEY = r"Environment"
DEFAULT_KEY_PATH = os.path.join(os.path.expanduser("~"), "yahoo_public.key")
# (環境変数名, 入力時の説明, 入力を隠すか, 既定値)
SETTINGS = [
    ("YAHOO_CLIENT_ID", "Client ID", False, None),
    ("YAHOO_CLIENT_SECRET", "シークレット(表示されません)", True, None),
    ("YAHOO_SELLER_ID", "ストアアカウント", False, None),
    ("YAHOO_REDIRECT_URI", "アプリに登録したコールバックURL", False, None),
    ("YAHOO_PUBLIC_KEY_PATH", "公開鍵ファイルのパス", False, DEFAULT_KEY_PATH),
    ("YAHOO_PUBLIC_KEY_VERSION", "公開鍵のバージョン(暗号鍵管理の画面の番号)", False, None),
]


def user_env(name):
    """このプロセスに無ければ、レジストリのユーザー環境変数から読む(ターミナルを開いた後に設定した場合)。"""
    if os.environ.get(name):
        return os.environ[name]
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, ENV_KEY) as k:
            return winreg.QueryValueEx(k, name)[0]
    except FileNotFoundError:
        return None


def set_user_env(name, value):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, ENV_KEY, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)


def broadcast_env_change():
    """これから開くターミナルやタスクスケジューラに新しい値を反映させる。"""
    HWND_BROADCAST, WM_SETTINGCHANGE, SMTO_ABORTIFHUNG = 0xFFFF, 0x001A, 0x0002
    ctypes.windll.user32.SendMessageTimeoutW(
        HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment", SMTO_ABORTIFHUNG, 5000, None)


def ensure_settings():
    """未設定の値だけ入力してもらい、ユーザー環境変数に保存する。"""
    for name, label, hidden, default in SETTINGS:
        if user_env(name):
            continue
        prompt = f"{name}: {label}" + (f" [{default}]" if default else "") + ": "
        value = (getpass.getpass(prompt) if hidden else input(prompt)).strip() or (default or "")
        if not value:
            sys.exit(f"{name} が空です。もう一度実行してください")
        set_user_env(name, value)
        os.environ[name] = value
    key_path = user_env("YAHOO_PUBLIC_KEY_PATH")
    if not os.path.isfile(key_path):
        sys.exit(f"公開鍵ファイルが見つかりません: {key_path}")


def main():
    ensure_settings()
    client_id = user_env("YAHOO_CLIENT_ID")
    client_secret = user_env("YAHOO_CLIENT_SECRET")
    redirect_uri = user_env("YAHOO_REDIRECT_URI")

    state = secrets.token_urlsafe(16)
    url = f"{AUTH_URL}?" + urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": "openid profile",
        "state": state,
        "bail": "1",
    })
    print("ブラウザでYahoo! JAPAN IDにログインし、同意してください。開かない場合は次のURLを開いてください:")
    print(url)
    webbrowser.open(url)

    returned = getpass.getpass("移動した先のページのURLを貼り付けて Enter(表示されません): ").strip()
    q = parse_qs(urlparse(returned).query)
    if "error" in q:
        sys.exit(f"認可されませんでした: {q['error'][0]} {q.get('error_description', [''])[0]}")
    if q.get("state", [None])[0] != state:
        sys.exit("state が一致しません。もう一度最初からやり直してください")
    code = q.get("code", [None])[0]
    if not code:
        sys.exit("URLに code がありません。移動した先のURLをまるごと貼り付けてください")

    r = requests.post(
        TOKEN_URL,
        auth=(client_id, client_secret),
        data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri},
        timeout=30,
    )
    if r.status_code != 200:
        # エラー応答には error / error_description のみで、トークン値は含まれない。
        sys.exit(f"トークン取得に失敗: HTTP {r.status_code} {r.text[:300]}")
    refresh_token = r.json().get("refresh_token")
    if not refresh_token:
        sys.exit("応答に refresh_token がありません")

    changed = refresh_token != user_env("YAHOO_REFRESH_TOKEN")
    set_user_env("YAHOO_REFRESH_TOKEN", refresh_token)
    # 期限の目安(秘密ではない)。定期実行側で期限切れが近いことを知らせるのに使う。
    set_user_env("YAHOO_AUTHORIZED_AT", date.today().isoformat())
    broadcast_env_change()

    print("YAHOO_REFRESH_TOKEN を保存しました(値は表示しません)。"
          + ("新しいトークンです。" if changed else "以前と同じトークンで、期限だけ延びました。"))
    print(f"次の再認可の期限: {date.today() + timedelta(days=28)} ごろ(公開鍵認証あり・4週間)")
    print("このあと開くターミナルから新しい値が使えます。")


if __name__ == "__main__":
    main()
