"""
auth.py - Google OAuth2 授權模組
處理 Google 帳號登入、Token 管理、自動更新
使用 Desktop App 授權流程 (本機端應用程式)
"""

import json
import os
import threading
import webbrowser
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


# ── Google Drive API 所需的授權範圍 ──
SCOPES = [
    "https://www.googleapis.com/auth/drive.file",   # 只存取此 App 建立的檔案
    "https://www.googleapis.com/auth/drive.metadata.readonly",  # 讀取雲端資料夾清單
]

TOKEN_FILE = Path.home() / ".drivesync" / "token.json"
CREDENTIALS_FILE = Path.home() / ".drivesync" / "credentials.json"

import os
# 重定向 URI：本機開發用 localhost，雲端部署從環境變數讀取
_default_redirect = "http://localhost:8765/oauth/callback"
REDIRECT_URI = os.environ.get("OAUTH_REDIRECT_URI", _default_redirect)


class AuthManager:
    """Google OAuth2 授權管理器"""

    def __init__(self):
        self._credentials: Optional[Credentials] = None
        self._auth_event = threading.Event()  # 用來等待授權完成的事件旗標
        self._pending_flow: Optional[Flow] = None
        self._load_saved_token()

    def _load_saved_token(self):
        """從本地端讀取已儲存的 Token，若過期則自動更新"""
        if not TOKEN_FILE.exists():
            return

        try:
            self._credentials = Credentials.from_authorized_user_file(
                str(TOKEN_FILE), SCOPES
            )
            # 若 Token 過期但有 Refresh Token，自動更新
            if self._credentials and self._credentials.expired and self._credentials.refresh_token:
                self._credentials.refresh(Request())
                self._save_token()
                print("[Auth] Token 已自動更新")
        except Exception as e:
            print(f"[Auth] 讀取 Token 失敗：{e}")
            self._credentials = None

    def _save_token(self):
        """將 Token 儲存到本地端（JSON 格式）"""
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(TOKEN_FILE, "w") as f:
            f.write(self._credentials.to_json())

    def is_authenticated(self) -> bool:
        """檢查是否已登入且 Token 有效"""
        if not self._credentials:
            return False
        if self._credentials.expired:
            try:
                self._credentials.refresh(Request())
                self._save_token()
                return True
            except Exception:
                return False
        return self._credentials.valid

    def get_auth_url(self) -> Optional[str]:
        """
        建立 Google OAuth2 授權 URL
        使用者需要在瀏覽器開啟此網址並允許授權
        """
        if not CREDENTIALS_FILE.exists():
            raise FileNotFoundError(
                f"找不到 credentials.json！\n"
                f"請至 Google Cloud Console 下載 OAuth2 憑證，\n"
                f"並放置於：{CREDENTIALS_FILE}"
            )

        self._pending_flow = Flow.from_client_secrets_file(
            str(CREDENTIALS_FILE),
            scopes=SCOPES,
            redirect_uri=REDIRECT_URI
        )

        # access_type=offline 可取得 Refresh Token，讓 App 無需重複登入
        auth_url, _ = self._pending_flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent"  # 強制顯示授權頁面（確保取得 Refresh Token）
        )
        return auth_url

    def handle_oauth_callback(self, code: str, state: str = None) -> bool:
        """
        處理 OAuth2 回調（使用者授權後 Google 會導向此處）
        code: 授權碼，用來換取 Access Token
        """
        if not self._pending_flow:
            raise ValueError("沒有進行中的授權流程，請先呼叫 get_auth_url()")

        try:
            # 用授權碼換取 Token
            self._pending_flow.fetch_token(code=code)
            self._credentials = self._pending_flow.credentials
            self._save_token()
            self._pending_flow = None
            self._auth_event.set()  # 通知等待中的執行緒授權已完成
            print("[Auth] 授權成功！Token 已儲存")
            return True
        except Exception as e:
            print(f"[Auth] 授權失敗：{e}")
            return False

    def logout(self):
        """登出：刪除本地 Token"""
        self._credentials = None
        if TOKEN_FILE.exists():
            TOKEN_FILE.unlink()
        print("[Auth] 已登出，Token 已刪除")

    def get_credentials(self) -> Optional[Credentials]:
        """取得有效的 Credentials 物件（供 Drive API 使用）"""
        if not self.is_authenticated():
            return None
        return self._credentials

    def get_user_info(self) -> Optional[dict]:
        """取得已登入使用者的基本資訊"""
        if not self.is_authenticated():
            return None
        try:
            service = build("oauth2", "v2", credentials=self._credentials)
            user_info = service.userinfo().get().execute()
            return {
                "email": user_info.get("email"),
                "name": user_info.get("name"),
                "picture": user_info.get("picture"),
            }
        except Exception as e:
            print(f"[Auth] 取得使用者資訊失敗：{e}")
            return None

    def setup_credentials_file(self, client_id: str, client_secret: str):
        """
        從 client_id 和 client_secret 建立 credentials.json
        （給不想手動放置 JSON 檔案的使用者）
        """
        creds_data = {
            "installed": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [REDIRECT_URI, "urn:ietf:wg:oauth:2.0:oob"]
            }
        }
        CREDENTIALS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(CREDENTIALS_FILE, "w") as f:
            json.dump(creds_data, f, indent=2)
        print(f"[Auth] credentials.json 已建立：{CREDENTIALS_FILE}")


# 全域單例授權管理器
auth_manager = AuthManager()
