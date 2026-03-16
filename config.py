"""
config.py - 設定儲存模組
負責讀寫使用者的資料夾映射設定、Token、同步日誌等資料
所有資料儲存在使用者家目錄下的 .drivesync 資料夾
"""

import json
import os
import sqlite3
from pathlib import Path
from datetime import datetime
from typing import List, Optional
from dataclasses import dataclass, asdict, field


# ── 資料夾路徑定義 ──
APP_DIR = Path.home() / ".drivesync"
CONFIG_FILE = APP_DIR / "config.json"
TOKEN_FILE = APP_DIR / "token.json"
DB_FILE = APP_DIR / "sync_log.db"


@dataclass
class FolderMapping:
    """一組資料夾映射：本地資料夾 <-> Google Drive 資料夾"""
    id: str                          # 唯一識別碼 (UUID)
    local_path: str                  # 本地端資料夾完整路徑
    drive_folder_id: str             # Google Drive 資料夾 ID
    drive_folder_name: str           # Google Drive 資料夾顯示名稱（供 UI 使用）
    enabled: bool = True             # 是否啟用此映射
    recursive: bool = True           # 是否監控子資料夾
    ignore_patterns: List[str] = field(default_factory=lambda: [
        "*.tmp", "*.temp", "~*", ".DS_Store", "Thumbs.db", "*.swp"
    ])
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class AppConfig:
    """全域應用程式設定"""
    mappings: List[FolderMapping] = field(default_factory=list)
    debounce_seconds: float = 3.0    # 防抖延遲（秒）：檔案停止變動後幾秒才上傳
    max_retries: int = 3             # 上傳失敗最大重試次數
    upload_chunk_size_mb: int = 10   # 分塊上傳大小（MB）
    notify_on_success: bool = True   # 成功時是否顯示系統通知
    notify_on_error: bool = True     # 失敗時是否顯示系統通知
    auto_start: bool = False         # 開機自動啟動
    server_port: int = 8765          # FastAPI 伺服器埠號
    version: str = "1.0.0"


class ConfigManager:
    """設定管理器：負責讀取、儲存、更新設定"""

    def __init__(self):
        # 確保應用程式資料夾存在
        APP_DIR.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self.config = self._load()

    def _init_db(self):
        """初始化 SQLite 同步日誌資料庫"""
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sync_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,       -- 事件時間
                mapping_id TEXT NOT NULL,       -- 對應的映射 ID
                local_path TEXT NOT NULL,       -- 本地檔案路徑
                drive_file_id TEXT,             -- Google Drive 檔案 ID
                action TEXT NOT NULL,           -- 動作：upload / update / error
                status TEXT NOT NULL,           -- 狀態：success / failed / pending
                error_msg TEXT,                 -- 錯誤訊息（如有）
                file_size INTEGER               -- 檔案大小（bytes）
            )
        """)
        conn.commit()
        conn.close()

    def _load(self) -> AppConfig:
        """從 JSON 檔案讀取設定，若不存在則建立預設設定"""
        if not CONFIG_FILE.exists():
            config = AppConfig()
            self._save(config)
            return config

        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)

            # 將 JSON 資料還原為 dataclass 物件
            mappings = []
            for m in data.get("mappings", []):
                mappings.append(FolderMapping(**m))

            data["mappings"] = mappings
            return AppConfig(**{k: v for k, v in data.items() if k != "mappings"},
                             mappings=mappings)
        except Exception as e:
            print(f"[Config] 讀取設定失敗，使用預設值：{e}")
            return AppConfig()

    def _save(self, config: AppConfig):
        """將設定儲存為 JSON 檔案"""
        data = asdict(config)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def save(self):
        """儲存目前設定"""
        self._save(self.config)

    def add_mapping(self, mapping: FolderMapping):
        """新增一組資料夾映射"""
        self.config.mappings.append(mapping)
        self.save()

    def remove_mapping(self, mapping_id: str) -> bool:
        """刪除指定映射，回傳是否成功"""
        original_count = len(self.config.mappings)
        self.config.mappings = [m for m in self.config.mappings if m.id != mapping_id]
        if len(self.config.mappings) < original_count:
            self.save()
            return True
        return False

    def toggle_mapping(self, mapping_id: str) -> Optional[bool]:
        """切換映射的啟用/停用狀態，回傳新狀態"""
        for m in self.config.mappings:
            if m.id == mapping_id:
                m.enabled = not m.enabled
                self.save()
                return m.enabled
        return None

    def log_sync(self, mapping_id: str, local_path: str, action: str,
                 status: str, drive_file_id: str = None,
                 error_msg: str = None, file_size: int = None):
        """記錄同步事件到 SQLite 資料庫"""
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO sync_log
            (timestamp, mapping_id, local_path, drive_file_id, action, status, error_msg, file_size)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (datetime.now().isoformat(), mapping_id, local_path,
              drive_file_id, action, status, error_msg, file_size))
        conn.commit()
        conn.close()

    def get_recent_logs(self, limit: int = 20) -> List[dict]:
        """取得最近的同步記錄"""
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row  # 讓結果可以用欄位名稱存取
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM sync_log
            ORDER BY id DESC LIMIT ?
        """, (limit,))
        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return rows

    def get_stats(self) -> dict:
        """取得同步統計數據"""
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*), SUM(file_size) FROM sync_log WHERE status='success'")
        count, total_size = cursor.fetchone()
        cursor.execute("SELECT COUNT(*) FROM sync_log WHERE status='failed'")
        error_count = cursor.fetchone()[0]
        conn.close()
        return {
            "total_uploaded": count or 0,
            "total_size_bytes": total_size or 0,
            "total_errors": error_count or 0,
        }


# 全域單例設定管理器
config_manager = ConfigManager()
