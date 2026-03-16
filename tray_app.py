"""
tray_app.py - 系統匣常駐程式
讓 DriveSync Pro 以背景常駐方式運行，提供右鍵選單快速操作
Windows / macOS 皆支援
"""

import threading
import webbrowser
import subprocess
import sys
import os
from pathlib import Path

try:
    import pystray
    from PIL import Image
    TRAY_AVAILABLE = True
except ImportError:
    TRAY_AVAILABLE = False
    print("[Tray] pystray 或 Pillow 未安裝，略過系統匣功能")

import uvicorn
from main import app
from config import config_manager
from auth import auth_manager


class TrayApp:
    """系統匣應用程式管理器"""

    def __init__(self):
        self._server_thread = None
        self._tray_icon = None
        self._running = False

    def _load_icon(self) -> Image.Image:
        """載入圖示，若找不到則動態產生"""
        icon_path = Path(__file__).parent.parent / "extension" / "icons" / "icon48.png"

        if icon_path.exists():
            return Image.open(icon_path)

        # 動態產生簡易圖示
        img = Image.new("RGBA", (48, 48), (232, 51, 42, 255))
        return img

    def _create_menu(self) -> "pystray.Menu":
        """建立右鍵選單"""
        return pystray.Menu(
            pystray.MenuItem(
                "DriveSync Pro v1.0",
                lambda: None,
                enabled=False  # 標題，不可點擊
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "📊 同步狀態",
                self._open_status
            ),
            pystray.MenuItem(
                "🔐 Google 授權",
                self._open_auth,
                visible=lambda _: not auth_manager.is_authenticated()
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "⏸ 暫停所有監控",
                self._pause_all,
                visible=lambda _: auth_manager.is_authenticated()
            ),
            pystray.MenuItem(
                "▶ 繼續所有監控",
                self._resume_all,
                visible=lambda _: auth_manager.is_authenticated()
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "📂 開啟設定資料夾",
                lambda: self._open_folder(str(Path.home() / ".drivesync"))
            ),
            pystray.MenuItem(
                "🌐 開啟 API 文件",
                lambda: webbrowser.open("http://localhost:8765/docs")
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "❌ 結束",
                self._quit
            ),
        )

    def _open_status(self, icon, item):
        """開啟 API 狀態頁面"""
        webbrowser.open("http://localhost:8765/sync/status")

    def _open_auth(self, icon, item):
        """開啟 Google 授權頁面"""
        try:
            import requests
            response = requests.get("http://localhost:8765/auth/url")
            auth_url = response.json().get("auth_url")
            if auth_url:
                webbrowser.open(auth_url)
        except Exception as e:
            print(f"[Tray] 取得授權 URL 失敗：{e}")

    def _pause_all(self, icon, item):
        """暫停所有監控"""
        from watcher import watcher_manager
        watcher_manager.stop_all()
        self._update_icon("paused")

    def _resume_all(self, icon, item):
        """恢復所有監控"""
        from watcher import watcher_manager
        watcher_manager.start_all()
        self._update_icon("active")

    def _open_folder(self, path: str):
        """開啟資料夾（跨平台）"""
        if sys.platform == "win32":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.run(["open", path])
        else:
            subprocess.run(["xdg-open", path])

    def _update_icon(self, state: str):
        """更新圖示狀態（可依不同狀態顯示不同顏色）"""
        # 此處可依需求動態改變圖示
        pass

    def _quit(self, icon, item):
        """結束程式"""
        print("[Tray] 正在關閉 DriveSync Pro...")
        self._running = False
        icon.stop()

    def _start_server(self):
        """在背景執行緒中啟動 FastAPI 伺服器"""
        port = config_manager.config.server_port
        uvicorn.run(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",  # 系統匣模式下減少 log 輸出
            access_log=False
        )

    def run(self):
        """啟動系統匣 App（同時啟動 FastAPI 後端）"""
        # 在背景執行緒啟動 FastAPI 伺服器
        self._server_thread = threading.Thread(
            target=self._start_server,
            daemon=True,
            name="FastAPI-Server"
        )
        self._server_thread.start()
        print(f"[Tray] 伺服器啟動中... http://localhost:{config_manager.config.server_port}")

        if not TRAY_AVAILABLE:
            print("[Tray] 無系統匣支援，伺服器在前景運行。按 Ctrl+C 結束。")
            try:
                self._server_thread.join()
            except KeyboardInterrupt:
                print("\n[Tray] 已關閉")
            return

        # 建立系統匣圖示
        icon_image = self._load_icon()
        self._tray_icon = pystray.Icon(
            name="DriveSync Pro",
            icon=icon_image,
            title="DriveSync Pro — 自動同步中",
            menu=self._create_menu()
        )

        print("[Tray] DriveSync Pro 已在系統匣中運行")
        self._running = True
        self._tray_icon.run()


def main():
    """程式進入點"""
    print("=" * 50)
    print("  DriveSync Pro — 系統匣模式")
    print("=" * 50)
    tray = TrayApp()
    tray.run()


if __name__ == "__main__":
    main()
