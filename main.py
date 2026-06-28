"""AI Agent FTP 服务 — 路由与入口"""

import os
import json
import time
import hashlib
from datetime import datetime
from pathlib import Path

from fastapi import (
    FastAPI,
    Request,
    HTTPException,
    Query,
    Form,
    File,
    UploadFile,
    status,
)
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    FileResponse,
    PlainTextResponse,
    JSONResponse,
)
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from config import settings
from utils import (
    ShareConfig,
    load_config,
    save_config,
    write_log,
    iter_all_logs,
    count_resource_views,
    compute_stats,
    find_share_by_vpath,
    validate_access_key,
    get_absolute_path,
    error_response,
    handle_mkdir,
    handle_upload_url,
    handle_content_upload,
    handle_delete,
    handle_rename,
    handle_put_upload,
    handle_multipart_upload,
    create_jwt_token,
    get_admin_from_cookie,
    mask_key,
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
    path = request.url.path
    # 管理页面的 GET 请求不记日志（登录/登出/配置变更由各自路由记录）
    if not (request.method == "GET" and path.startswith("/admin")):
        write_log(
            {
                "action": "request",
                "method": request.method,
                "path": path,
                "status": response.status_code,
                "duration_ms": duration_ms,
                "ip": request.client.host if request.client else "unknown",
            }
        )
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
        return templates.TemplateResponse(request, "admin_login.jinja", {"error": None})
    configs = load_config()
    stats = compute_stats()
    return templates.TemplateResponse(
        request, "admin_dashboard.jinja", {"shares": configs, "stats": stats}
    )


@app.post("/admin/login")
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
            "list": list_perm,
            "read": read_perm,
            "write": write_perm,
            "delete": delete_perm,
            "rename": rename_perm,
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
    return compute_stats()


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
    all_logs = iter_all_logs()
    if action:
        all_logs = [l for l in all_logs if l.get("action") == action]
    if path_filter:
        all_logs = [
            l for l in all_logs if path_filter.lower() in l.get("path", "").lower()
        ]
    if ip_filter:
        all_logs = [l for l in all_logs if ip_filter in l.get("ip", "")]
    if keyword:
        kw = keyword.lower()
        all_logs = [
            l for l in all_logs if kw in json.dumps(l, ensure_ascii=False).lower()
        ]
    if date_from:
        try:
            dt_from = datetime.fromisoformat(date_from)
            all_logs = [
                l for l in all_logs if datetime.fromisoformat(l["timestamp"]) >= dt_from
            ]
        except (ValueError, KeyError):
            pass
    if date_to:
        try:
            dt_to = datetime.fromisoformat(date_to + "T23:59:59")
            all_logs = [
                l for l in all_logs if datetime.fromisoformat(l["timestamp"]) <= dt_to
            ]
        except (ValueError, KeyError):
            pass
    total_logs = len(all_logs)
    offset = (page - 1) * per_page
    logs = list(reversed(all_logs))[offset : offset + per_page]
    total_pages = max(1, (total_logs + per_page - 1) // per_page)
    return templates.TemplateResponse(
        request,
        "admin_logs.jinja",
        {
            "logs": logs,
            "page": page,
            "total_pages": total_pages,
            "total_logs": total_logs,
            "action": action or "",
            "path_filter": path_filter or "",
            "ip_filter": ip_filter or "",
            "keyword": keyword or "",
            "date_from": date_from or "",
            "date_to": date_to or "",
        },
    )


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
    help: str = Query(None),
):
    json_mode = json == 1
    help_level, help_hint = _resolve_help_level(help) if help else (None, "")
    share = find_share_by_vpath("/" + path)
    if not share:
        if json_mode:
            return JSONResponse(
                status_code=404,
                content={"success": False, "error": "No share matched", "code": 404},
            )
        raise HTTPException(status_code=404, detail="No share matched")
    if not validate_access_key(share, key):
        write_log(
            {
                "action": "access_denied",
                "path": path,
                "ip": request.client.host,
                "key": mask_key(key),
            }
        )
        if json_mode:
            return JSONResponse(
                status_code=403,
                content={"success": False, "error": "Invalid access key", "code": 403},
            )
        raise HTTPException(status_code=403, detail="Invalid access key")
    abs_path = get_absolute_path(share, "/" + path)

    # 操作分发
    if mkdir == 1:
        return await handle_mkdir(
            share, abs_path, path, request.client.host, key, json_mode
        )
    if upload_url:
        fn = os.path.basename(path.rstrip("/")) or ""
        return await handle_upload_url(
            share, abs_path, upload_url, fn, path, request.client.host, key, json_mode
        )
    if content is not None:
        fn = os.path.basename(path.rstrip("/")) or ""
        return await handle_content_upload(
            share, abs_path, content, fn, path, request.client.host, key, json_mode
        )
    if delete == 1:
        return await handle_delete(
            share, abs_path, path, request.client.host, key, json_mode
        )
    if rename_to:
        return await handle_rename(
            share,
            abs_path,
            rename_to,
            path,
            request.client.host,
            key,
            json_mode=json_mode,
        )
    if move_to:
        return await handle_rename(
            share,
            abs_path,
            None,
            path,
            request.client.host,
            key,
            move_target=move_to,
            json_mode=json_mode,
        )

    # 常规文件/目录访问
    def _resp(template: str, ctx: dict, status: int = 200):
        """返回响应，help 模式下自动附加帮助文档"""
        if help_level:
            if json_mode:
                ctx["help"] = HELP_MD
                return JSONResponse(content=ctx, status_code=status)
            # HTML 模式：渲染主模板 → 注入帮助 → 返回合并 HTML
            from fastapi.responses import HTMLResponse
            main_html = templates.get_template(template).render(request=request, **ctx)
            help_html = templates.get_template("help.jinja").render({"level": help_level, "hint": help_hint})
            combined = main_html.replace("</body>", f'<div style="margin:24px auto;max-width:800px"><hr>{help_html}</div></body>')
            return HTMLResponse(combined)
        if json_mode:
            return JSONResponse(content=ctx, status_code=status)
        return templates.TemplateResponse(request, template, ctx)

    log_entry = {
        "action": "access",
        "path": path,
        "ip": request.client.host,
        "key": mask_key(key),
        "operation": "unknown",
    }
    if not abs_path.exists():
        write_log({**log_entry, "status": 404})
        if json_mode:
            return JSONResponse(
                status_code=404,
                content={
                    "success": False,
                    "error": "File or directory not found",
                    "code": 404,
                },
            )
        raise HTTPException(status_code=404, detail="File or directory not found")

    if abs_path.is_dir():
        if not share.permissions.get("list", False):
            write_log({**log_entry, "operation": "list_denied", "status": 403})
            if json_mode:
                return JSONResponse(
                    status_code=403,
                    content={
                        "success": False,
                        "error": "Listing not allowed",
                        "code": 403,
                    },
                )
            raise HTTPException(status_code=403, detail="Listing not allowed")
        entries = []
        for entry in sorted(abs_path.iterdir()):
            if entry.name.startswith("."):
                continue
            st = entry.stat()
            entry_type = "dir" if entry.is_dir() else "file"
            entries.append(
                {
                    "name": entry.name,
                    "type": entry_type,
                    "size": st.st_size if entry_type == "file" else 0,
                    "modified": datetime.fromtimestamp(st.st_mtime).isoformat(),
                }
            )
        log_entry["operation"] = "list_dir"
        write_log(log_entry)
        ctx = {"type": "directory", "path": path, "entries": entries, "share_name": share.name}
        if json_mode:
            return _resp(None, ctx)
        ctx.update({"key": key, "can_write": share.permissions.get("write", False),
                     "can_delete": share.permissions.get("delete", False),
                     "can_rename": share.permissions.get("rename", False)})
        return _resp("listing.jinja", ctx)
    else:
        if not share.permissions.get("read", False):
            write_log({**log_entry, "operation": "read_denied", "status": 403})
            if json_mode:
                return JSONResponse(
                    status_code=403,
                    content={
                        "success": False,
                        "error": "Read not allowed",
                        "code": 403,
                    },
                )
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
        if json_mode:
            return _resp(None, {
                "type": "file", "path": path, "filename": abs_path.name,
                "size": file_stat.st_size,
                "modified": datetime.fromtimestamp(file_stat.st_mtime).isoformat(),
            })
        content_preview = ""
        is_text = False
        if file_stat.st_size <= settings.preview_max_size:
            try:
                with open(abs_path, "r", encoding="utf-8") as f:
                    content_preview = f.read()
                is_text = True
            except (UnicodeDecodeError, PermissionError, MemoryError):
                pass
        view_count = count_resource_views("/" + path)
        line_count = content_preview.count("\n") + 1 if is_text else 0
        return _resp("file_display.jinja", {
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
    request: Request,
    path: str,
    key: str = Query(None),
    json: int = Query(0, alias="json"),
    filename: str = Query(None),
):
    json_mode = json == 1
    share = find_share_by_vpath("/" + path)
    if not share:
        return error_response("No share matched", 404, json_mode)
    if not validate_access_key(share, key):
        write_log(
            {
                "action": "access_denied",
                "path": path,
                "ip": request.client.host,
                "key": mask_key(key),
            }
        )
        return error_response("Invalid access key", 403, json_mode)
    abs_path = get_absolute_path(share, "/" + path)
    return await handle_put_upload(
        request, share, abs_path, path, key, json_mode, filename
    )


@app.post("/s/{path:path}")
async def serve_content_post(
    request: Request,
    path: str,
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
            write_log(
                {
                    "action": "access_denied",
                    "path": path,
                    "ip": request.client.host,
                    "key": mask_key(key),
                }
            )
            return error_response("Invalid access key", 403, json_mode)
        abs_path = get_absolute_path(share, "/" + path)
        return await handle_multipart_upload(
            request, share, abs_path, path, key, file, json_mode
        )
    return await serve_content(
        request=request,
        path=path,
        key=key,
        raw=0,
        download=0,
        json=json,
        mkdir=mkdir,
        upload_url=None,
        content=None,
        delete=0,
        rename_to=None,
        move_to=None,
    )


@app.delete("/s/{path:path}")
async def serve_content_delete(
    request: Request,
    path: str,
    key: str = Query(None),
    json: int = Query(0, alias="json"),
):
    return await serve_content(
        request=request,
        path=path,
        key=key,
        raw=0,
        download=0,
        json=json,
        mkdir=0,
        upload_url=None,
        content=None,
        delete=1,
        rename_to=None,
        move_to=None,
    )


# ------------------- 权限查询 -------------------
@app.get("/perm/{path:path}")
async def query_permission(path: str, key: str = Query(...)):
    """查询指定路径的访问权限（无需对应权限即可查询）"""
    share = find_share_by_vpath("/" + path)
    if not share:
        return JSONResponse(
            status_code=404, content={"success": False, "error": "No share matched"}
        )
    if not validate_access_key(share, key):
        return JSONResponse(
            status_code=403, content={"success": False, "error": "Invalid access key"}
        )
    return {
        "path": "/" + path,
        "share_name": share.name,
        "permissions": share.permissions,
    }


# ------------------- Markdown 帮助文本 -------------------
HELP_MD = """# AI Agent FTP 服务

本服务为 AI Agent 设计的类 FTP 加密文件服务。
所有写操作经过 SSRF 防护、路径穿越检测和速率限制。

## 端点

| 方法 | 路径 | 说明 | 权限 |
|------|------|------|------|
| GET | /s/{path}?key=xxx | 查看文件或目录 | read/list |
| GET | ?mkdir=1 | 创建目录 | write |
| GET | ?upload_url=URL | 从 URL 拉取 | write |
| GET | ?content=数据 | 上传文本(64KB上限) | write |
| GET | ?delete=1 | 删除 | delete |
| GET | ?rename_to=新名 | 重命名 | rename |
| GET | ?move_to=/目标 | 移动 | rename |
| PUT | /s/{path}?key=xxx | 上传(Body原始内容) | write |
| POST | /s/{path}?key=xxx | 上传(multipart) | write |
| DELETE | /s/{path}?key=xxx | 删除 | delete |
| GET | /perm/{path}?key=xxx | 查询权限 | — |

## 通用参数
- &raw=1 原始内容  &download=1 下载  &json=1 JSON输出

## 安全限制
- 上传最大 500MB | content= 最大 64KB
- upload_url 仅 HTTPS，拦截内网(SSRF防护)
- 写操作 60 次/分钟(速率限制)
- 文件名净化，拒绝路径穿越

## 限制 Agent 示例(仅GET)
```
mkdir:  GET /s/data?key=abc&mkdir=1
upload: GET /s/data?key=abc&upload_url=https://example.com/file.zip
content:GET /s/data/note.txt?key=abc&content=Hello+World
delete: GET /s/data/old.txt?key=abc&delete=1
rename: GET /s/data/old.txt?key=abc&rename_to=new.txt
perm:   GET /perm/data?key=abc
```

## 管理
- /admin 管理界面  /admin/stats 统计  /health 健康检查
"""


def _resolve_help_level(level: str) -> tuple[str, str]:
    """校验 help level 参数，返回 (有效level, 提示消息)"""
    valid = {"basic", "full", "md", "1", "2"}
    if level in valid:
        return (level, "")
    # 别名映射
    alias = {"1": "basic", "2": "full"}
    if level in alias:
        return (alias[level], "")
    # 无效值：使用默认 basic，提示取值范围
    hint = f"Invalid help level '{level}', valid: basic, full, md (or 1, 2). Using 'basic'."
    return ("basic", hint)


# ------------------- 帮助页 -------------------
@app.get("/help")
async def help_page(
    request: Request,
    level: str = "basic",
    format: str = Query("html"),
):
    cleaned, hint = _resolve_help_level(level)
    if cleaned in ("md",):
        format = "md"
    context = {"level": cleaned, "hint": hint}
    if format == "md":
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(HELP_MD)
    return templates.TemplateResponse(request, "help.jinja", context)


# ------------------- 启动入口 -------------------
if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=settings.listen_host,
        port=settings.listen_port,
        reload=settings.debug,
    )
