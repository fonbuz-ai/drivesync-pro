"""
drive_client.py - Google Drive API 封裝模組
負責所有與 Google Drive 的操作：上傳、更新、查詢資料夾
"""

import os
import mimetypes
from pathlib import Path
from typing import Optional, List, Dict

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from auth import auth_manager


# 分塊上傳的塊大小：10MB（適合大檔案，避免記憶體溢出）
CHUNK_SIZE = 10 * 1024 * 1024


class DriveClient:
    """Google Drive 操作封裝類別"""

    def __init__(self):
        self._service = None

    def _get_service(self):
        """取得（或重建）Google Drive API 服務物件"""
        credentials = auth_manager.get_credentials()
        if not credentials:
            raise PermissionError("尚未授權 Google 帳號，請先完成 OAuth2 登入")

        # 若 service 不存在或 credentials 有更新，重新建立
        if self._service is None:
            self._service = build("drive", "v3", credentials=credentials)
        return self._service

    def list_folders(self, parent_id: str = "root") -> List[Dict]:
        """
        列出指定資料夾下的所有子資料夾
        parent_id: 父資料夾 ID，預設為 Google Drive 根目錄
        """
        service = self._get_service()
        try:
            query = (
                f"'{parent_id}' in parents "
                f"and mimeType='application/vnd.google-apps.folder' "
                f"and trashed=false"
            )
            result = service.files().list(
                q=query,
                fields="files(id, name, modifiedTime)",
                orderBy="name",
                pageSize=100
            ).execute()

            return result.get("files", [])
        except HttpError as e:
            print(f"[Drive] 列出資料夾失敗：{e}")
            raise

    def create_folder(self, name: str, parent_id: str = "root") -> Dict:
        """在 Google Drive 建立新資料夾"""
        service = self._get_service()
        file_metadata = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id]
        }
        folder = service.files().create(
            body=file_metadata,
            fields="id, name"
        ).execute()
        print(f"[Drive] 已建立資料夾：{name} (ID: {folder['id']})")
        return folder

    def find_file_in_folder(self, filename: str, folder_id: str) -> Optional[str]:
        """
        在指定資料夾中搜尋同名檔案
        回傳：找到時回傳 file_id，否則回傳 None
        """
        service = self._get_service()
        # 對檔案名稱中的單引號做跳脫處理，避免查詢語法錯誤
        safe_name = filename.replace("'", "\\'")
        query = (
            f"name='{safe_name}' "
            f"and '{folder_id}' in parents "
            f"and trashed=false"
        )
        result = service.files().list(
            q=query,
            fields="files(id, name)",
            pageSize=1
        ).execute()

        files = result.get("files", [])
        return files[0]["id"] if files else None

    def upload_file(
        self,
        local_path: str,
        folder_id: str,
        on_progress=None
    ) -> Dict:
        """
        上傳新檔案到 Google Drive 指定資料夾
        若同名檔案已存在，改為更新（update）
        local_path: 本地檔案完整路徑
        folder_id: 目標 Google Drive 資料夾 ID
        on_progress: 進度回呼函式 (bytes_uploaded, total_bytes)
        """
        service = self._get_service()
        file_path = Path(local_path)
        filename = file_path.name
        file_size = file_path.stat().st_size

        # 自動偵測 MIME 類型，若偵測不到則使用通用二進位類型
        mime_type, _ = mimetypes.guess_type(local_path)
        if not mime_type:
            mime_type = "application/octet-stream"

        # 建立媒體上傳物件（支援分塊上傳大檔案）
        media = MediaFileUpload(
            local_path,
            mimetype=mime_type,
            chunksize=CHUNK_SIZE,
            resumable=True  # 可恢復上傳：網路中斷後可繼續
        )

        # 檢查是否已有同名檔案（決定要 create 還是 update）
        existing_id = self.find_file_in_folder(filename, folder_id)

        try:
            if existing_id:
                # ── 更新已存在的檔案 ──
                print(f"[Drive] 更新檔案：{filename} (ID: {existing_id})")
                request = service.files().update(
                    fileId=existing_id,
                    media_body=media,
                    fields="id, name, size, modifiedTime"
                )
                action = "update"
            else:
                # ── 上傳新檔案 ──
                print(f"[Drive] 上傳新檔案：{filename}")
                file_metadata = {
                    "name": filename,
                    "parents": [folder_id]
                }
                request = service.files().create(
                    body=file_metadata,
                    media_body=media,
                    fields="id, name, size, modifiedTime"
                )
                action = "upload"

            # 執行分塊上傳（支援進度回報）
            response = None
            while response is None:
                status, response = request.next_chunk()
                if status and on_progress:
                    on_progress(
                        int(status.resumable_progress),
                        file_size
                    )

            print(f"[Drive] {action} 完成：{filename} ({file_size:,} bytes)")
            return {
                "id": response.get("id"),
                "name": response.get("name"),
                "size": file_size,
                "action": action
            }

        except HttpError as e:
            print(f"[Drive] 上傳失敗 {filename}：{e}")
            raise

    def get_folder_path(self, folder_id: str) -> str:
        """取得資料夾的完整路徑字串（例如：MyDrive/工作/備份）"""
        service = self._get_service()
        parts = []
        current_id = folder_id

        # 向上追溯父資料夾，最多 10 層（避免無限迴圈）
        for _ in range(10):
            try:
                file = service.files().get(
                    fileId=current_id,
                    fields="id, name, parents"
                ).execute()
                parts.append(file["name"])

                parents = file.get("parents", [])
                if not parents:
                    break
                current_id = parents[0]

                # 到達根目錄就停止
                if current_id == "root":
                    break
            except HttpError:
                break

        parts.reverse()
        return " / ".join(parts) if parts else "未知路徑"


# 全域單例 Drive 客戶端
drive_client = DriveClient()
