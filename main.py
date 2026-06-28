"""AI Agent FTP 服务 — 路由与入口"""

import os
import json
import time
import hashlib
from datetime import datetime
from pathlib import Path

from fastapi import (
    FastAPI, Request, HTTPException, Query, Form, File, UploadFile, status,
)
from fastapi.responses import (
    HTMLResponse, RedirectResponse, FileResponse, PlainTextResponse, JSONResponse,
)
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from config import settings
from utils import (
    ShareConfig, load_config, save_config,
    write_log, _iter_all_logs, _list_log_shards, count_resource_views,
    find_share_by_vpath, validate_access_key, get_absolute_path,
    require_write_permission, require_delete_permission, require_rename_permission,
    error_response, success_response,
    handle_mkdir, handle_upload_url, handle_content_upload,
    handle_delete, handle_rename, handle_put_upload, handle_multipart_upload,
    create_jwt_token, get_admin_from_cookie,
)

# ------------------- FastAPI 应用 -------------------
app = FastAPI(title="AI Agent FTP Service", version="1.0")

# ------------------- CORS 中间件 -------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ------------------- 请求日志中间件 -------------------
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    duration_ms = int((time.time() - start) * 1000)
    write_log({
        "action": "request",
        "method": request.method,
        "path": request.url.path,
        "status": response.status_code,
        "duration_ms": duration_ms,
        "ip": request.client.host if request.client else "unknown",
    })
    return response


# ------------------- 健康检查 -------------------
@app.get("/health")
async def health_check():
    return {"status": "ok", "service": "AIWebFTP", "version": "1.0"}


# ------------------- 管理页面 -------------------
@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    admin = get_admin_from_cookie(request)
    if not admin:
        return templates.TemplateResponse(request, "admin_login.html", {"error": None})
    configs = load_config()
    shards = _list_log_shards()
    total_logs = 0
    total_size = 0
    for shard in shards:
        total_size += shard.stat().st_size
        try:
            with open(shard, "r", encoding="utf-8") as f:
                total_logs += sum(1 for _ in f)
        except FileNotFoundError:
            continue
    stats = {
        "shares_count": len(configs),
        "log_entries": total_logs,
        "log_shards": len(shards),
        "log_size_mb": round(total_size / (1024 * 1024), 2),
    }
    return templates.TemplateResponse(
        request, "admin_dashboard.html", {"shares": configs, "stats": stats}
    )


@app.post("/admin/login")
async def admin_login(request: Request, key: str = Form(...)):
    if key != settings.admin_key:
        return templates.TemplateResponse(
            request, "admin_login.html", {"error": "Invalid admin key"}
        )
    token = create_jwt_token({"sub": "admin"})
    resp = RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)
    resp.set_cookie(
        "admin_token", token, httponly=True,
        max_age=settings.access_token_expire_minutes * 60,
    )
    write_log({"action": "admin_login", "ip": request.client.host})
    return resp


@app.get("/admin/logout")
async def admin_logout():
    resp = RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)
    resp.delete_cookie("admin_token")
    return resp


@app.post("/admin/shares/add")
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
):
    admin = get_admin_from_cookie(request)
    if not admin:
        raise HTTPException(status_code=401, detail="Unauthorized")
    configs = load_config()
    new_id = hashlib.md5(f"{name}{virtual_path}{time.time()}".encode()).hexdigest()[:8]
    new_share = ShareConfig(
        id=new_id,
        name=name,
        virtual_path=virtual_path,
        real_path=os.path.realpath(real_path),
        permissions={
            "list": list_perm, "read": read_perm,
            "write": write_perm, "delete": delete_perm, "rename": rename_perm,
        },
        access_key=access_key,
    )
    configs.append(new_share)
    save_config(configs)
    write_log(
        {"action": "share_added", "id": new_id, "name": name, "ip": request.client.host}
    )
    return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/shares/delete/{share_id}")
async def delete_share(request: Request, share_id: str):
    admin = get_admin_from_cookie(request)
    if not admin:
        raise HTTPException(status_code=401, detail="Unauthorized")
    configs = load_config()
    new_configs = [c for c in configs if c.id != share_id]
    if len(new_configs) == len(configs):
        raise HTTPException(status_code=404, detail="Share not found")
    save_config(new_configs)
    write_log({"action": "share_deleted", "id": share_id, "ip": request.client.host})
    return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/admin/stats")
async def admin_stats(request: Request):
    admin = get_admin_from_cookie(request)
    if not admin:
        raise HTTPException(status_code=401, detail="Unauthorized")
    configs = load_config()
    shards = _list_log_shards()
    total_logs = 0
    total_size = 0
    for shard in shards:
        total_size += shard.stat().st_size
        try:
            with open(shard, "r", encoding="utf-8") as f:
                total_logs += sum(1 for _ in f)
        except FileNotFoundError:
            continue
    return {
        "shares_count": len(configs),
        "log_entries": total_logs,
        "log_shards": len(shards),
        "log_size_bytes": total_size,
        "log_size_mb": round(total_size / (1024 * 1024), 2),
    }


@app.get("/admin/logs", response_class=HTMLResponse)
async def view_logs(
    request: Request,
    page: int = 1,
    per_page: int = 50,
    action: str = Query(None),
    path_filter: str = Query(None, alias="path"),
    ip_filter: str = Query(None, alias="ip"),
    keyword: str = Query(None),
    date_from: str = Query(None),
    date_to: str = Query(None),
):
    admin = get_admin_from_cookie(request)
    if not admin:
        raise HTTPException(status_code=401, detail="Unauthorized")
    all_logs = _iter_all_logs()
    if action:
        all_logs = [l for l in all_logs if l.get("action") == action]
    if path_filter:
        all_logs = [l for l in all_logs if path_filter.lower() in l.get("path", "").lower()]
    if ip_filter:
        all_logs = [l for l in all_logs if ip_filter in l.get("ip", "")]
    if keyword:
        kw = keyword.lower()
        all_logs = [l for l in all_logs if kw in json.dumps(l, ensure_ascii=False).lower()]
    if date_from:
        try:
            dt_from = datetime.fromisoformat(date_from)
            all_logs = [l for l in all_logs if datetime.fromisoformat(l["timestamp"]) >= dt_from]
        except (ValueError, KeyError):
            pass
    if date_to:
        try:
            dt_to = datetime.fromisoformat(date_to + "T23:59:59")
            all_logs = [l for l in all_logs if datetime.fromisoformat(l["timestamp"]) <= dt_to]
        except (ValueError, KeyError):
            pass
    total_logs = len(all_logs)
    offset = (page - 1) * per_page
    logs = list(reversed(all_logs))[offset: offset + per_page]
    total_pages = max(1, (total_logs + per_page - 1) // per_page)
    return templates.TemplateResponse(request, "admin_logs.html", {
        "logs": logs, "page": page, "total_pages": total_pages, "total_logs": total_logs,
        "action": action or "", "path_filter": path_filter or "",
        "ip_filter": ip_filter or "", "keyword": keyword or "",
        "date_from": date_from or "", "date_to": date_to or "",
    })


# ------------------- 文件服务 -------------------
@app.get("/s/{path:path}")
async def serve_content(
    request: Request,
    path: str,
    key: str = Query(None),
    raw: int = Query(0),
    download: int = Query(0),
    json: int = Query(0, alias="json"),
    mkdir: int = Query(0),
    upload_url: str = Query(None),
    content: str = Query(None),
    delete: int = Query(0),
    rename_to: str = Query(None),
    move_to: str = Query(None),
):
    json_mode = json == 1
    share = find_share_by_vpath("/" + path)
    if not share:
        if json_mode:
            return JSONResponse(status_code=404, content={"success": False, "error": "No share matched", "code": 404})
        raise HTTPException(status_code=404, detail="No share matched")
    if not validate_access_key(share, key):
        write_log({"action": "access_denied", "path": path, "ip": request.client.host,
                    "key": key[:4] + "***" if key else "None"})
        if json_mode:
            return JSONResponse(status_code=403, content={"success": False, "error": "Invalid access key", "code": 403})
        raise HTTPException(status_code=403, detail="Invalid access key")
    abs_path = get_absolute_path(share, "/" + path)

    # 操作分发
    if mkdir == 1:
        return await handle_mkdir(share, abs_path, path, request.client.host, key, json_mode)
    if upload_url:
        fn = os.path.basename(path.rstrip("/")) or ""
        return await handle_upload_url(share, abs_path, upload_url, fn, path, request.client.host, key, json_mode)
    if content is not None:
        fn = os.path.basename(path.rstrip("/")) or ""
        return await handle_content_upload(share, abs_path, content, fn, path, request.client.host, key, json_mode)
    if delete == 1:
        return await handle_delete(share, abs_path, path, request.client.host, key, json_mode)
    if rename_to:
        return await handle_rename(share, abs_path, rename_to, path, request.client.host, key, json_mode=json_mode)
    if move_to:
        return await handle_rename(share, abs_path, None, path, request.client.host, key, move_target=move_to, json_mode=json_mode)

    # 常规文件/目录访问
    log_entry = {"action": "access", "path": path, "ip": request.client.host,
                  "key": key[:4] + "***", "operation": "unknown"}
    if not abs_path.exists():
        write_log({**log_entry, "status": 404})
        if json_mode:
            return JSONResponse(status_code=404, content={"success": False, "error": "File or directory not found", "code": 404})
        raise HTTPException(status_code=404, detail="File or directory not found")

    if abs_path.is_dir():
        if not share.permissions.get("list", False):
            write_log({**log_entry, "operation": "list_denied", "status": 403})
            if json_mode:
                return JSONResponse(status_code=403, content={"success": False, "error": "Listing not allowed", "code": 403})
            raise HTTPException(status_code=403, detail="Listing not allowed")
        entries = []
        for entry in sorted(abs_path.iterdir()):
            if entry.name.startswith("."):
                continue
            entry_type = "dir" if entry.is_dir() else "file"
            entries.append({
                "name": entry.name, "type": entry_type,
                "size": entry.stat().st_size if entry_type == "file" else 0,
                "modified": datetime.fromtimestamp(entry.stat().st_mtime).isoformat(),
            })
        log_entry["operation"] = "list_dir"
        write_log(log_entry)
        if json_mode:
            return JSONResponse(content={"type": "directory", "path": path, "entries": entries, "share_name": share.name})
        return templates.TemplateResponse(request, "listing.html", {
            "path": path, "entries": entries, "key": key, "share_name": share.name,
            "can_write": share.permissions.get("write", False),
            "can_delete": share.permissions.get("delete", False),
            "can_rename": share.permissions.get("rename", False),
        })
    else:
        if not share.permissions.get("read", False):
            write_log({**log_entry, "operation": "read_denied", "status": 403})
            if json_mode:
                return JSONResponse(status_code=403, content={"success": False, "error": "Read not allowed", "code": 403})
            raise HTTPException(status_code=403, detail="Read not allowed")
        log_entry["operation"] = "read_file"
        write_log(log_entry)
        if download == 1:
            return FileResponse(abs_path, filename=abs_path.name)
        if raw == 1:
            try:
                return PlainTextResponse(abs_path.read_text(encoding="utf-8"))
            except UnicodeDecodeError:
                return FileResponse(abs_path, filename=abs_path.name)
        file_stat = abs_path.stat()
        content_preview = ""
        is_text = False
        try:
            with open(abs_path, "r", encoding="utf-8") as f:
                content_preview = f.read()
            is_text = True
        except (UnicodeDecodeError, PermissionError):
            pass
        if json_mode:
            return JSONResponse(content={
                "type": "file", "path": path, "filename": abs_path.name,
                "size": file_stat.st_size,
                "modified": datetime.fromtimestamp(file_stat.st_mtime).isoformat(),
                "content": content_preview if is_text else None, "is_text": is_text,
            })
        view_count = count_resource_views("/" + path)
        line_count = content_preview.count("\n") if is_text else 0
        return templates.TemplateResponse(request, "file_display.html", {
            "path": path, "filename": abs_path.name,
            "size": file_stat.st_size, "line_count": line_count,
            "modified": datetime.fromtimestamp(file_stat.st_mtime).isoformat(),
            "content": content_preview, "is_text": is_text,
            "key": key, "view_count": view_count,
            "can_write": share.permissions.get("write", False),
            "can_delete": share.permissions.get("delete", False),
            "can_rename": share.permissions.get("rename", False),
        })


@app.put("/s/{path:path}")
async def serve_content_put(
    request: Request, path: str,
    key: str = Query(None),
    json: int = Query(0, alias="json"),
    filename: str = Query(None),
):
    json_mode = json == 1
    share = find_share_by_vpath("/" + path)
    if not share:
        return error_response("No share matched", 404, json_mode)
    if not validate_access_key(share, key):
        write_log({"action": "access_denied", "path": path, "ip": request.client.host,
                    "key": key[:4] + "***" if key else "None"})
        return error_response("Invalid access key", 403, json_mode)
    abs_path = get_absolute_path(share, "/" + path)
    return await handle_put_upload(request, share, abs_path, path, key, json_mode, filename)


@app.post("/s/{path:path}")
async def serve_content_post(
    request: Request, path: str,
    key: str = Query(None),
    mkdir: int = Query(0),
    json: int = Query(0, alias="json"),
    file: UploadFile = File(None),
):
    json_mode = json == 1
    if file:
        share = find_share_by_vpath("/" + path)
        if not share:
            return error_response("No share matched", 404, json_mode)
        if not validate_access_key(share, key):
            write_log({"action": "access_denied", "path": path, "ip": request.client.host,
                        "key": key[:4] + "***" if key else "None"})
            return error_response("Invalid access key", 403, json_mode)
        abs_path = get_absolute_path(share, "/" + path)
        return await handle_multipart_upload(request, share, abs_path, path, key, file, json_mode)
    return await serve_content(
        request=request, path=path, key=key,
        raw=0, download=0, json=json, mkdir=mkdir,
        upload_url=None, content=None, delete=0, rename_to=None, move_to=None,
    )


@app.delete("/s/{path:path}")
async def serve_content_delete(
    request: Request, path: str,
    key: str = Query(None),
    json: int = Query(0, alias="json"),
):
    return await serve_content(
        request=request, path=path, key=key,
        raw=0, download=0, json=json, mkdir=0,
        upload_url=None, content=None, delete=1, rename_to=None, move_to=None,
    )


# ------------------- 帮助页 -------------------
@app.get("/help", response_class=HTMLResponse)
async def help_page(request: Request, level: str = "basic"):
    return templates.TemplateResponse(request, "help.html", {"level": level})


# ------------------- 启动入口 -------------------
if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
