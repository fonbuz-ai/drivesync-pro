"""
queue_manager.py - 上傳佇列管理器
負責防抖處理、上傳排程、失敗重試
防抖：檔案持續被修改時，等待其停止變動後才上傳（避免大量重複上傳）
"""

import threading
import time
import fnmatch
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Optional, Set
from enum import Enum


class UploadStatus(str, Enum):
    PENDING = "pending"      # 等待中（防抖計時）
    UPLOADING = "uploading"  # 上傳中
    SUCCESS = "success"      # 上傳成功
    FAILED = "failed"        # 上傳失敗（已用盡重試次數）
    RETRYING = "retrying"    # 重試中


@dataclass
class UploadTask:
    """一個待上傳任務"""
    local_path: str           # 本地檔案路徑
    mapping_id: str           # 所屬的資料夾映射 ID
    drive_folder_id: str      # 目標 Drive 資料夾 ID
    status: UploadStatus = UploadStatus.PENDING
    retry_count: int = 0      # 已重試次數
    created_at: float = field(default_factory=time.time)
    last_modified: float = field(default_factory=time.time)
    error_msg: Optional[str] = None
    drive_file_id: Optional[str] = None
    file_size: int = 0


class QueueManager:
    """
    上傳佇列管理器
    使用「防抖」技術：每當檔案有變動，就重設計時器，
    等計時器到期（檔案停止變動後 N 秒），才真正執行上傳
    """

    def __init__(
        self,
        debounce_seconds: float = 3.0,
        max_retries: int = 3,
        on_status_change: Callable = None
    ):
        self.debounce_seconds = debounce_seconds
        self.max_retries = max_retries
        self.on_status_change = on_status_change  # 狀態更新時的回呼函式

        # 防抖計時器字典：{local_path: Timer 物件}
        self._debounce_timers: Dict[str, threading.Timer] = {}
        # 上傳任務字典：{local_path: UploadTask}
        self._tasks: Dict[str, UploadTask] = {}
        # 目前正在上傳的路徑集合
        self._uploading: Set[str] = set()
        # 執行緒鎖（保護共享資料）
        self._lock = threading.Lock()
        # 是否正在運行
        self._running = True

        print(f"[Queue] 初始化完成 (防抖={debounce_seconds}s, 最大重試={max_retries}次)")

    def enqueue(
        self,
        local_path: str,
        mapping_id: str,
        drive_folder_id: str,
        ignore_patterns: list = None
    ):
        """
        將檔案加入待上傳佇列（若已存在則重置防抖計時器）
        local_path: 觸發事件的本地檔案路徑
        """
        if not self._running:
            return

        # 檢查檔案是否應被忽略
        if ignore_patterns and self._should_ignore(local_path, ignore_patterns):
            print(f"[Queue] 忽略檔案（符合過濾規則）：{local_path}")
            return

        # 檢查檔案是否存在且可讀
        path_obj = Path(local_path)
        if not path_obj.exists() or not path_obj.is_file():
            return

        # 取得檔案大小
        try:
            file_size = path_obj.stat().st_size
        except OSError:
            return

        with self._lock:
            # 若此路徑已有防抖計時器，取消它（重新計時）
            if local_path in self._debounce_timers:
                self._debounce_timers[local_path].cancel()
                print(f"[Queue] 重置防抖計時器：{path_obj.name}")

            # 建立或更新任務
            if local_path in self._tasks:
                self._tasks[local_path].last_modified = time.time()
                self._tasks[local_path].file_size = file_size
                self._tasks[local_path].status = UploadStatus.PENDING
            else:
                self._tasks[local_path] = UploadTask(
                    local_path=local_path,
                    mapping_id=mapping_id,
                    drive_folder_id=drive_folder_id,
                    file_size=file_size
                )
                print(f"[Queue] 新增任務：{path_obj.name} ({file_size:,} bytes)")

            # 建立新的防抖計時器：N 秒後執行上傳
            timer = threading.Timer(
                self.debounce_seconds,
                self._execute_upload,
                args=[local_path]
            )
            timer.daemon = True
            timer.start()
            self._debounce_timers[local_path] = timer

    def _should_ignore(self, local_path: str, patterns: list) -> bool:
        """檢查檔案是否符合忽略規則（支援萬用字元）"""
        filename = Path(local_path).name
        return any(fnmatch.fnmatch(filename, pattern) for pattern in patterns)

    def _execute_upload(self, local_path: str):
        """
        防抖計時結束後實際執行上傳
        在獨立執行緒中運行，不阻塞主程式
        """
        with self._lock:
            # 清除已完成的計時器
            self._debounce_timers.pop(local_path, None)

            task = self._tasks.get(local_path)
            if not task or local_path in self._uploading:
                return

            self._uploading.add(local_path)
            task.status = UploadStatus.UPLOADING

        self._notify_status_change(local_path, task)

        # 在新執行緒中上傳，不阻塞佇列處理
        upload_thread = threading.Thread(
            target=self._do_upload,
            args=[local_path],
            daemon=True
        )
        upload_thread.start()

    def _do_upload(self, local_path: str):
        """實際執行上傳，包含重試邏輯"""
        from drive_client import drive_client
        from config import config_manager

        with self._lock:
            task = self._tasks.get(local_path)
        if not task:
            return

        for attempt in range(self.max_retries + 1):
            try:
                if attempt > 0:
                    # 指數退避：第 1 次等 2s，第 2 次等 4s，第 3 次等 8s
                    wait_time = 2 ** attempt
                    print(f"[Queue] 第 {attempt} 次重試，等待 {wait_time}s：{Path(local_path).name}")
                    time.sleep(wait_time)
                    with self._lock:
                        task.status = UploadStatus.RETRYING
                        task.retry_count = attempt

                # 取得最新檔案大小
                if Path(local_path).exists():
                    task.file_size = Path(local_path).stat().st_size

                # 執行上傳
                result = drive_client.upload_file(
                    local_path=local_path,
                    folder_id=task.drive_folder_id
                )

                # ── 上傳成功 ──
                with self._lock:
                    task.status = UploadStatus.SUCCESS
                    task.drive_file_id = result["id"]
                    task.error_msg = None

                # 記錄到資料庫
                config_manager.log_sync(
                    mapping_id=task.mapping_id,
                    local_path=local_path,
                    action=result["action"],
                    status="success",
                    drive_file_id=result["id"],
                    file_size=task.file_size
                )
                print(f"[Queue] ✓ 上傳成功：{Path(local_path).name}")
                self._notify_status_change(local_path, task)
                break  # 成功就離開重試迴圈

            except Exception as e:
                print(f"[Queue] 上傳錯誤 (attempt {attempt+1})：{e}")
                if attempt >= self.max_retries:
                    # ── 已用盡重試次數，標記失敗 ──
                    with self._lock:
                        task.status = UploadStatus.FAILED
                        task.error_msg = str(e)

                    config_manager.log_sync(
                        mapping_id=task.mapping_id,
                        local_path=local_path,
                        action="upload",
                        status="failed",
                        error_msg=str(e),
                        file_size=task.file_size
                    )
                    print(f"[Queue] ✗ 上傳失敗（已達最大重試次數）：{Path(local_path).name}")
                    self._notify_status_change(local_path, task)

        with self._lock:
            self._uploading.discard(local_path)

    def _notify_status_change(self, local_path: str, task: UploadTask):
        """通知外部狀態已變更（例如更新 Extension 顯示）"""
        if self.on_status_change:
            try:
                self.on_status_change(local_path, task)
            except Exception:
                pass

    def get_status(self) -> list:
        """取得目前所有任務的狀態清單"""
        with self._lock:
            return [
                {
                    "path": task.local_path,
                    "filename": Path(task.local_path).name,
                    "status": task.status.value,
                    "retry_count": task.retry_count,
                    "file_size": task.file_size,
                    "error": task.error_msg,
                    "created_at": task.created_at
                }
                for task in self._tasks.values()
            ]

    def get_pending_count(self) -> int:
        """取得等待中的任務數量"""
        with self._lock:
            return sum(
                1 for t in self._tasks.values()
                if t.status in (UploadStatus.PENDING, UploadStatus.UPLOADING, UploadStatus.RETRYING)
            )

    def stop(self):
        """停止佇列，取消所有待執行的計時器"""
        self._running = False
        with self._lock:
            for timer in self._debounce_timers.values():
                timer.cancel()
            self._debounce_timers.clear()
        print("[Queue] 佇列已停止")


# 全域佇列實例（在 main.py 中初始化）
queue_manager: Optional[QueueManager] = None
