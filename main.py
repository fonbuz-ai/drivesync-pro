"""
main.py - DriveSync Pro 後端主程式
FastAPI 伺服器：提供 REST API 給 Chrome Extension 呼叫
預設監聽 http://localhost:8765
"""

import uuid
import asyncio
import threading
from contextlib import asynccontextmanager
from typing import List, Optional

# 雲端部署：從環境變數自動建立 credentials.json
try:
    import config_cloud
except Exception:
    pass

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from config import config_manager, FolderMapping, AppConfig
from auth import auth_manager
from drive_client import drive_client
from watcher import watcher_manager
from queue_manager import QueueManager, queue_manager as _queue_manager


# ────────────────────────────────────────────
# Pydantic 模型（定義 API 請求/回應的資料格式）
# ────────────────────────────────────────────

class AddMappingRequest(BaseModel):
    local_path: str          # 本地端資料夾路徑
    drive_folder_id: str     # Google Drive 資料夾 ID
    drive_folder_name: str   # Drive 資料夾顯示名稱
    recursive: bool = True   # 是否監控子資料夾

class UpdateSettingsRequest(BaseModel):
    debounce_seconds: Optional[float] = None
    max_retries: Optional[int] = None
    notify_on_success: Optional[bool] = None
    notify_on_error: Optional[bool] = None

class SetupCredentialsRequest(BaseModel):
    client_id: str
    client_secret: str


# ────────────────────────────────────────────
# 應用程式生命週期管理
# ────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """應用程式啟動/關閉時的處理邏輯"""
    # ── 啟動時 ──
    print("=" * 50)
    print("  DriveSync Pro Backend v1.0.0")
    print("  http://localhost:8765")
    print("=" * 50)

    # 初始化上傳佇列
    global queue_manager_instance
    queue_manager_instance = QueueManager(
        debounce_seconds=config_manager.config.debounce_seconds,
        max_retries=config_manager.config.max_retries
    )

    # 設定監控管理器的佇列
    watcher_manager.set_queue_manager(queue_manager_instance)

    # 若已授權，在背景執行緒啟動監控（避免阻塞 async lifespan）
    if auth_manager.is_authenticated():
        t = threading.Thread(target=watcher_manager.start_all, daemon=True)
        t.start()
        print("[Main] 監控已在背景啟動")
    else:
        print("[Main] 尚未授權 Google 帳號，請先完成登入")

    yield  # ← 伺服器運行期間

    # ── 關閉時 ──
    watcher_manager.stop_all()
    if queue_manager_instance:
        queue_manager_instance.stop()
    print("[Main] DriveSync Pro 已關閉")


# ────────────────────────────────────────────
# FastAPI App 設定
# ────────────────────────────────────────────

app = FastAPI(
    title="DriveSync Pro API",
    description="本地資料夾自動同步 Google Drive 後端服務",
    version="1.0.0",
    lifespan=lifespan
)

# 允許 Chrome Extension 跨來源請求（CORS）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 全域佇列實例（由 lifespan 初始化）
queue_manager_instance: Optional[QueueManager] = None


# ────────────────────────────────────────────
# API 路由定義
# ────────────────────────────────────────────

@app.get("/health")
async def health_check():
    """健康檢查端點：Extension 用來確認後端服務是否在執行"""
    return {
        "status": "ok",
        "version": "1.0.0",
        "authenticated": auth_manager.is_authenticated(),
        "active_watchers": len(watcher_manager.get_active_watchers()),
        "pending_uploads": queue_manager_instance.get_pending_count() if queue_manager_instance else 0,
    }


# ── 授權相關 ──

@app.get("/auth/url")
async def get_auth_url():
    """取得 Google OAuth2 授權 URL，使用者需在瀏覽器開啟此網址"""
    try:
        url = auth_manager.get_auth_url()
        return {"auth_url": url}
    except FileNotFoundError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/oauth/callback")
async def oauth_callback(code: str, state: str = None):
    """
    Google OAuth2 回調端點
    使用者授權後，Google 會帶著 code 導向這裡
    """
    success = auth_manager.handle_oauth_callback(code, state)
    if success:
        # 授權成功後在背景啟動監控
        watcher_manager.set_queue_manager(queue_manager_instance)
        t = threading.Thread(target=watcher_manager.start_all, daemon=True)
        t.start()
        # 顯示成功頁面
        return HTMLResponse(content="""
        <html><body style="font-family:sans-serif;text-align:center;margin-top:60px;">
        <h2 style="color:#28a745">✅ 授權成功！</h2>
        <p>DriveSync Pro 已取得 Google Drive 存取權限。</p>
        <p>請關閉此視窗，回到 Chrome Extension 繼續設定。</p>
        </body></html>
        """)
    else:
        raise HTTPException(status_code=400, detail="OAuth2 授權失敗")


@app.get("/auth/status")
async def auth_status():
    """取得目前授權狀態與使用者資訊"""
    authenticated = auth_manager.is_authenticated()
    user_info = auth_manager.get_user_info() if authenticated else None
    return {
        "authenticated": authenticated,
        "user": user_info
    }


@app.post("/auth/logout")
async def logout():
    """登出：刪除本地 Token 並停止所有監控"""
    watcher_manager.stop_all()
    auth_manager.logout()
    return {"message": "已成功登出"}


@app.post("/auth/setup")
async def setup_credentials(req: SetupCredentialsRequest):
    """從 client_id/secret 建立 credentials.json（方便新手設定）"""
    try:
        auth_manager.setup_credentials_file(req.client_id, req.client_secret)
        return {"message": "credentials.json 已建立，請呼叫 /auth/url 開始授權"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── 資料夾映射 CRUD ──

@app.get("/mappings")
async def list_mappings():
    """取得所有資料夾映射設定"""
    return {
        "mappings": [
            {
                "id": m.id,
                "local_path": m.local_path,
                "drive_folder_id": m.drive_folder_id,
                "drive_folder_name": m.drive_folder_name,
                "enabled": m.enabled,
                "recursive": m.recursive,
                "ignore_patterns": m.ignore_patterns,
                "created_at": m.created_at
            }
            for m in config_manager.config.mappings
        ]
    }


@app.post("/mappings")
async def add_mapping(req: AddMappingRequest):
    """新增一組資料夾映射，並立即啟動監控"""
    from pathlib import Path

    # 驗證本地路徑
    if not Path(req.local_path).exists():
        raise HTTPException(status_code=400, detail=f"本地路徑不存在：{req.local_path}")

    if not Path(req.local_path).is_dir():
        raise HTTPException(status_code=400, detail="指定路徑必須是資料夾")

    # 建立新映射物件
    mapping = FolderMapping(
        id=str(uuid.uuid4()),
        local_path=req.local_path,
        drive_folder_id=req.drive_folder_id,
        drive_folder_name=req.drive_folder_name,
        recursive=req.recursive
    )

    config_manager.add_mapping(mapping)

    # 立即在背景啟動此映射的監控
    watcher_manager.set_queue_manager(queue_manager_instance)
    t = threading.Thread(target=watcher_manager.start_watching, args=[mapping], daemon=True)
    t.start()

    return {"message": "映射已新增並啟動監控", "mapping_id": mapping.id}


@app.delete("/mappings/{mapping_id}")
async def remove_mapping(mapping_id: str):
    """刪除指定映射並停止對應的監控"""
    watcher_manager.stop_watching(mapping_id)
    success = config_manager.remove_mapping(mapping_id)
    if not success:
        raise HTTPException(status_code=404, detail="找不到指定映射")
    return {"message": "映射已刪除"}


@app.patch("/mappings/{mapping_id}/toggle")
async def toggle_mapping(mapping_id: str):
    """切換映射的啟用/停用狀態"""
    new_state = config_manager.toggle_mapping(mapping_id)
    if new_state is None:
        raise HTTPException(status_code=404, detail="找不到指定映射")

    # 重啟監控（啟用則開始，停用則停止）
    watcher_manager.restart_mapping(mapping_id)

    return {"enabled": new_state, "message": f"監控已{'啟用' if new_state else '停用'}"}


# ── Google Drive 資料夾瀏覽 ──

@app.get("/drive/folders")
async def list_drive_folders(parent_id: str = "root"):
    """列出 Google Drive 指定資料夾下的子資料夾"""
    if not auth_manager.is_authenticated():
        raise HTTPException(status_code=401, detail="請先完成 Google 帳號授權")
    try:
        folders = drive_client.list_folders(parent_id)
        return {"folders": folders}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/drive/folders")
async def create_drive_folder(name: str, parent_id: str = "root"):
    """在 Google Drive 建立新資料夾"""
    if not auth_manager.is_authenticated():
        raise HTTPException(status_code=401, detail="請先完成 Google 帳號授權")
    try:
        folder = drive_client.create_folder(name, parent_id)
        return folder
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── 同步狀態 & 日誌 ──

@app.get("/sync/status")
async def sync_status():
    """取得目前同步狀態（佇列中的任務）"""
    tasks = queue_manager_instance.get_status() if queue_manager_instance else []
    watchers = watcher_manager.get_active_watchers()
    return {
        "tasks": tasks[-20:],  # 最近 20 筆
        "active_watchers": len(watchers),
        "watchers": watchers,
        "pending_count": queue_manager_instance.get_pending_count() if queue_manager_instance else 0
    }


@app.get("/sync/logs")
async def get_sync_logs(limit: int = 20):
    """取得同步歷史記錄"""
    logs = config_manager.get_recent_logs(limit)
    return {"logs": logs}


@app.get("/sync/stats")
async def get_stats():
    """取得同步統計資料"""
    return config_manager.get_stats()


# ── 設定更新 ──

@app.patch("/settings")
async def update_settings(req: UpdateSettingsRequest):
    """更新全域設定"""
    cfg = config_manager.config

    if req.debounce_seconds is not None:
        cfg.debounce_seconds = req.debounce_seconds
        if queue_manager_instance:
            queue_manager_instance.debounce_seconds = req.debounce_seconds

    if req.max_retries is not None:
        cfg.max_retries = req.max_retries
        if queue_manager_instance:
            queue_manager_instance.max_retries = req.max_retries

    if req.notify_on_success is not None:
        cfg.notify_on_success = req.notify_on_success

    if req.notify_on_error is not None:
        cfg.notify_on_error = req.notify_on_error

    config_manager.save()
    return {"message": "設定已更新", "config": {
        "debounce_seconds": cfg.debounce_seconds,
        "max_retries": cfg.max_retries,
        "notify_on_success": cfg.notify_on_success,
        "notify_on_error": cfg.notify_on_error,
    }}


@app.get("/settings")
async def get_settings():
    """取得目前設定"""
    cfg = config_manager.config
    return {
        "debounce_seconds": cfg.debounce_seconds,
        "max_retries": cfg.max_retries,
        "notify_on_success": cfg.notify_on_success,
        "notify_on_error": cfg.notify_on_error,
        "server_port": cfg.server_port,
    }


# ────────────────────────────────────────────
# 程式進入點
# ────────────────────────────────────────────

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", config_manager.config.server_port))
    host = os.environ.get("HOST", "0.0.0.0")  # 雲端部署需要 0.0.0.0
    uvicorn.run(
        "main:app",
        host=host,
        port=port,
        reload=False,
        log_level="info"
    )
