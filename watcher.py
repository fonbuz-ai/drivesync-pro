"""
watcher.py - 檔案系統監控模組
使用 watchdog 套件監控本地資料夾的變更（新增、修改）
每個資料夾映射對應一個獨立的 Observer
"""

import threading
from pathlib import Path
from typing import Dict, Optional

from watchdog.observers import Observer
from watchdog.events import (
    FileSystemEventHandler,
    FileCreatedEvent,
    FileModifiedEvent,
    FileMovedEvent
)

from config import config_manager, FolderMapping


class DriveEventHandler(FileSystemEventHandler):
    """
    watchdog 事件處理器
    當監控的資料夾有檔案新增或修改時，此類別的方法會被自動呼叫
    """

    def __init__(self, mapping: FolderMapping, queue_manager_ref):
        super().__init__()
        self.mapping = mapping
        self.queue_manager = queue_manager_ref
        print(f"[Watcher] 開始監控：{mapping.local_path}")

    def on_created(self, event: FileCreatedEvent):
        """當有新檔案被建立時觸發"""
        if event.is_directory:
            return  # 忽略資料夾本身的建立事件（只處理檔案）
        print(f"[Watcher] 偵測到新檔案：{event.src_path}")
        self._handle_file_event(event.src_path)

    def on_modified(self, event: FileModifiedEvent):
        """當檔案被修改時觸發"""
        if event.is_directory:
            return
        print(f"[Watcher] 偵測到檔案修改：{event.src_path}")
        self._handle_file_event(event.src_path)

    def on_moved(self, event: FileMovedEvent):
        """當檔案被移動/重新命名時，以目的地為新檔案處理"""
        if event.is_directory:
            return
        print(f"[Watcher] 偵測到檔案移動：{event.dest_path}")
        self._handle_file_event(event.dest_path)

    def _handle_file_event(self, file_path: str):
        """
        統一處理所有檔案事件
        計算此檔案在 Drive 中應放置的資料夾位置（保持目錄結構）
        """
        if not self.mapping.enabled:
            return

        path_obj = Path(file_path)

        # 計算此檔案相對於監控根目錄的相對路徑
        # 例如：監控 /home/user/docs，檔案是 /home/user/docs/2024/report.xlsx
        # 則 relative_path 是 2024/report.xlsx
        try:
            relative_path = path_obj.relative_to(self.mapping.local_path)
        except ValueError:
            return  # 不在監控範圍內

        # 確定目標 Drive 資料夾 ID（若有子資料夾則需建立對應結構）
        target_folder_id = self._get_or_create_drive_folder(relative_path)

        # 加入上傳佇列
        self.queue_manager.enqueue(
            local_path=str(file_path),
            mapping_id=self.mapping.id,
            drive_folder_id=target_folder_id,
            ignore_patterns=self.mapping.ignore_patterns
        )

    def _get_or_create_drive_folder(self, relative_path: Path) -> str:
        """
        根據相對路徑，在 Google Drive 建立對應的資料夾結構
        例如：相對路徑是 2024/Q1/report.xlsx
        則在 Drive 建立：目標資料夾/2024/Q1/
        回傳最終資料夾的 ID
        """
        from drive_client import drive_client

        # 取得相對路徑中的資料夾部分（排除檔案名稱）
        folder_parts = relative_path.parts[:-1]  # 不含最後的檔案名稱

        if not folder_parts:
            # 檔案直接在根目錄，不需要建立子資料夾
            return self.mapping.drive_folder_id

        # 逐層建立資料夾
        current_parent_id = self.mapping.drive_folder_id
        for folder_name in folder_parts:
            try:
                # 先檢查是否已存在此資料夾
                existing_folders = drive_client.list_folders(current_parent_id)
                existing = next(
                    (f for f in existing_folders if f["name"] == folder_name),
                    None
                )

                if existing:
                    current_parent_id = existing["id"]
                else:
                    # 不存在則建立
                    new_folder = drive_client.create_folder(folder_name, current_parent_id)
                    current_parent_id = new_folder["id"]
            except Exception as e:
                print(f"[Watcher] 建立 Drive 子資料夾失敗：{e}，改用根目錄")
                return self.mapping.drive_folder_id

        return current_parent_id


class WatcherManager:
    """
    監控管理器
    管理所有 watchdog Observer，每個資料夾映射對應一個 Observer
    """

    def __init__(self):
        # {mapping_id: Observer 物件}
        self._observers: Dict[str, Observer] = {}
        self._lock = threading.Lock()
        self._queue_manager = None  # 由 main.py 設定

    def set_queue_manager(self, qm):
        """設定上傳佇列管理器（需在啟動前設定）"""
        self._queue_manager = qm

    def start_watching(self, mapping: FolderMapping) -> bool:
        """
        啟動對指定資料夾映射的監控
        回傳 True 表示啟動成功
        """
        if not self._queue_manager:
            print("[Watcher] 錯誤：queue_manager 尚未設定")
            return False

        # 驗證本地路徑存在
        local_path = Path(mapping.local_path)
        if not local_path.exists():
            print(f"[Watcher] 路徑不存在，略過監控：{mapping.local_path}")
            return False

        if not local_path.is_dir():
            print(f"[Watcher] 指定路徑不是資料夾：{mapping.local_path}")
            return False

        with self._lock:
            # 若已有此映射的 Observer，先停止舊的
            self.stop_watching(mapping.id)

            # 建立事件處理器與 Observer
            event_handler = DriveEventHandler(mapping, self._queue_manager)
            observer = Observer()
            observer.schedule(
                event_handler,
                path=str(local_path),
                recursive=mapping.recursive  # 是否遞歸監控子資料夾
            )
            observer.start()
            self._observers[mapping.id] = observer
            print(f"[Watcher] ✓ 啟動監控成功：{mapping.local_path} (recursive={mapping.recursive})")
            return True

    def stop_watching(self, mapping_id: str):
        """停止指定映射的監控"""
        with self._lock:
            observer = self._observers.pop(mapping_id, None)
            if observer:
                observer.stop()
                observer.join(timeout=2)
                print(f"[Watcher] 已停止監控 mapping_id={mapping_id}")

    def start_all(self):
        """依據目前設定，啟動所有啟用的映射監控"""
        enabled_mappings = [
            m for m in config_manager.config.mappings if m.enabled
        ]
        print(f"[Watcher] 啟動 {len(enabled_mappings)} 個監控任務...")
        for mapping in enabled_mappings:
            self.start_watching(mapping)

    def stop_all(self):
        """停止所有監控"""
        with self._lock:
            mapping_ids = list(self._observers.keys())

        for mid in mapping_ids:
            self.stop_watching(mid)
        print("[Watcher] 所有監控已停止")

    def get_active_watchers(self) -> list:
        """取得目前所有活躍的監控清單"""
        with self._lock:
            return [
                {
                    "mapping_id": mid,
                    "is_alive": obs.is_alive()
                }
                for mid, obs in self._observers.items()
            ]

    def restart_mapping(self, mapping_id: str):
        """重啟指定映射的監控（設定更新後使用）"""
        mapping = next(
            (m for m in config_manager.config.mappings if m.id == mapping_id),
            None
        )
        if mapping:
            self.stop_watching(mapping_id)
            if mapping.enabled:
                self.start_watching(mapping)


# 全域監控管理器
watcher_manager = WatcherManager()
