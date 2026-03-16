"""
config_cloud.py - 雲端部署設定覆寫
讀取環境變數中的 Google OAuth credentials（避免把 credentials.json 上傳到 GitHub）
在 Render.com 的 Environment Variables 中設定以下變數：
  GOOGLE_CLIENT_ID     = 你的 client_id
  GOOGLE_CLIENT_SECRET = 你的 client_secret
"""
import os
import json
from pathlib import Path

def setup_cloud_credentials():
    """若有環境變數，自動建立 credentials.json"""
    client_id = os.environ.get("GOOGLE_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")

    if not client_id or not client_secret:
        return  # 本機開發模式，用本地的 credentials.json

    app_dir = Path.home() / ".drivesync"
    app_dir.mkdir(parents=True, exist_ok=True)
    creds_file = app_dir / "credentials.json"

    redirect_uri = os.environ.get(
        "OAUTH_REDIRECT_URI",
        "http://localhost:8765/oauth/callback"
    )

    creds = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "redirect_uris": [redirect_uri]
        }
    }
    with open(creds_file, "w") as f:
        json.dump(creds, f)
    print(f"[CloudConfig] credentials.json created from env vars")

# 啟動時自動執行
setup_cloud_credentials()
