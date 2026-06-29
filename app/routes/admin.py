"""管理面板路由 — /admin/*"""

import os
import json
import hashlib
import time
import asyncio
from datetime import datetime

from fastapi import APIRouter, Request, Depends, HTTPException, Form, status, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import settings
from app.log_stream import ring_buffer, manager
from app.utils import (
    ShareConfig,
    load_config,
    save_config,
    write_log,
    iter_all_logs,
    compute_stats,
    create_jwt_token,
    get_admin_from_cookie,
)
from app.common import templates
from app.deps import require_admin
from app.schemas import LogFilter

router = APIRouter(tags=["admin"])


@router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    admin = get_admin_from_cookie(request)
    if not admin:
        return templates.TemplateResponse(request, "admin_login.jinja", {"error": None})
    configs = load_config()
    stats = compute_stats()
    now_str = datetime.now().strftime("%Y-%m-%d")
    return templates.TemplateResponse(
        request,
        "admin_dashboard.jinja",
        {"shares": configs, "stats": stats, "now": now_str},
    )


@router.post("/admin/login")
async def admin_login(request: Request, key: str = Form(...)):
    if key != settings.admin_key:
        return templates.TemplateResponse(
            request, "admin_login.jinja", {"error": "Invalid admin key"}
        )
    token = create_jwt_token({"sub": "admin"})
    resp = RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)
    resp.set_cookie(
        "admin_token",
        token,
        httponly=True,
        max_age=settings.access_token_expire_minutes * 60,
    )
    write_log({"action": "admin_login", "ip": request.client.host})
    return resp


@router.get("/admin/logout")
async def admin_logout():
    resp = RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)
    resp.delete_cookie("admin_token")
    return resp


@router.post("/admin/shares/add")
async def add_share(
    request: Request,
    name: str = Form(...),
    virtual_path: str = Form(...),
    real_path: str = Form(...),
    list_perm: bool = Form(True),
    read_perm: bool = Form(True),
    write_perm: bool = Form(False),
    delete_perm: bool = Form(False),
    rename_perm: bool = Form(False),
    access_key: str = Form(...),
    enabled: bool = Form(True),
    access_key_expires: str = Form(""),
    admin=Depends(require_admin),
):
    configs = load_config()
    new_id = hashlib.md5(f"{name}{virtual_path}{time.time()}".encode()).hexdigest()[:8]
    new_share = ShareConfig(
        id=new_id,
        name=name,
        virtual_path=virtual_path,
        real_path=os.path.realpath(real_path),
        permissions={
            "list": list_perm,
            "read": read_perm,
            "write": write_perm,
            "delete": delete_perm,
            "rename": rename_perm,
        },
        access_key=access_key,
        enabled=enabled,
        access_key_expires=access_key_expires,
    )
    configs.append(new_share)
    save_config(configs)
    write_log(
        {"action": "share_added", "id": new_id, "name": name, "ip": request.client.host}
    )
    return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/admin/shares/delete/{share_id}")
async def delete_share(request: Request, share_id: str, admin=Depends(require_admin)):
    configs = load_config()
    new_configs = [c for c in configs if c.id != share_id]
    if len(new_configs) == len(configs):
        raise HTTPException(status_code=404, detail="Share not found")
    save_config(new_configs)
    write_log({"action": "share_deleted", "id": share_id, "ip": request.client.host})
    return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/admin/shares/update/{share_id}")
async def update_share(
    request: Request,
    share_id: str,
    enabled: bool = Form(False),
    access_key: str = Form(""),
    access_key_expires: str = Form(""),
    list_perm: bool = Form(False),
    read_perm: bool = Form(False),
    write_perm: bool = Form(False),
    delete_perm: bool = Form(False),
    rename_perm: bool = Form(False),
    admin=Depends(require_admin),
):
    configs = load_config()
    for share in configs:
        if share.id == share_id:
            share.enabled = enabled
            if access_key:
                share.access_key = access_key
            share.access_key_expires = access_key_expires
            share.permissions = {
                "list": list_perm,
                "read": read_perm,
                "write": write_perm,
                "delete": delete_perm,
                "rename": rename_perm,
            }
            save_config(configs)
            write_log(
                {"action": "share_updated", "id": share_id, "ip": request.client.host}
            )
            break
    return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/admin/stats")
async def admin_stats(admin=Depends(require_admin)):
    return compute_stats()


@router.get("/admin/logs", response_class=HTMLResponse)
async def view_logs(
    request: Request,
    f: LogFilter = Depends(),
    admin=Depends(require_admin),
):
    all_logs = iter_all_logs()
    if f.action:
        all_logs = [l for l in all_logs if l.get("action") == f.action]
    if f.path_filter:
        all_logs = [
            l for l in all_logs if f.path_filter.lower() in l.get("path", "").lower()
        ]
    if f.ip_filter:
        all_logs = [l for l in all_logs if f.ip_filter in l.get("ip", "")]
    if f.keyword:
        kw = f.keyword.lower()
        all_logs = [
            l for l in all_logs if kw in json.dumps(l, ensure_ascii=False).lower()
        ]
    if f.date_from:
        try:
            dt_from = datetime.fromisoformat(f.date_from)
            all_logs = [
                l for l in all_logs if datetime.fromisoformat(l["timestamp"]) >= dt_from
            ]
        except (ValueError, KeyError):
            pass
    if f.date_to:
        try:
            dt_to = datetime.fromisoformat(f.date_to + "T23:59:59")
            all_logs = [
                l for l in all_logs if datetime.fromisoformat(l["timestamp"]) <= dt_to
            ]
        except (ValueError, KeyError):
            pass
    total_logs = len(all_logs)
    offset = (f.page - 1) * f.per_page
    logs = list(reversed(all_logs))[offset : offset + f.per_page]
    total_pages = max(1, (total_logs + f.per_page - 1) // f.per_page)
    return templates.TemplateResponse(
        request,
        "admin_logs.jinja",
        {
            "logs": logs,
            "page": f.page,
            "total_pages": total_pages,
            "total_logs": total_logs,
            "action": f.action or "",
            "path_filter": f.path_filter or "",
            "ip_filter": f.ip_filter or "",
            "keyword": f.keyword or "",
            "date_from": f.date_from or "",
            "date_to": f.date_to or "",
        },
    )


@router.get("/admin/logs/live", response_class=HTMLResponse)
async def admin_logs_live(request: Request):
    """实时日志流页面"""
    admin = get_admin_from_cookie(request)
    if not admin:
        return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request, "admin_logs_live.jinja", {})


@router.websocket("/admin/logs/stream")
async def log_stream_endpoint(websocket: WebSocket):
    """WebSocket 实时日志推送"""
    # ── JWT 鉴权 ──
    token = websocket.cookies.get("admin_token")
    if not token:
        await websocket.close(code=4001)
        return

    try:
        from jose import jwt

        payload = jwt.decode(
            token, settings.secret_key, algorithms=[settings.jwt_algorithm]
        )
        if payload.get("sub") != "admin":
            await websocket.close(code=4001)
            return
    except Exception:
        await websocket.close(code=4001)
        return

    # ── 连接建立 ──
    await manager.connect(websocket)

    # 发送初始历史（最新的在前）
    entries = ring_buffer.snapshot()
    entries.reverse()
    await websocket.send_json({"type": "init", "entries": entries})

    try:
        while True:
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=30)
                # 暂不处理客户端消息，预留未来扩展
            except asyncio.TimeoutError:
                # 心跳
                await websocket.send_json({"type": "ping"})
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        manager.disconnect(websocket)
