"""文件服务路由 — /s/{path}, /perm/{path}"""

import os
from datetime import datetime
from urllib.parse import urlparse

from fastapi import APIRouter, Request, HTTPException, Query, File, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, JSONResponse

from app.config import settings
from app.utils import (
    find_share_by_vpath, check_access, get_absolute_path,
    count_resource_views, error_response,
    handle_mkdir, handle_upload_url, handle_content_upload,
    handle_delete, handle_rename, handle_put_upload, handle_multipart_upload,
    write_log, mask_key,
)
from app.common import templates, HELP_MD, resolve_help_level

router = APIRouter(tags=["files"])


@router.get("/s/{path:path}")
async def serve_content(
    request: Request, path: str,
    key: str = Query(None),
    raw: int = Query(0), download: int = Query(0),
    json: int = Query(0, alias="json"),
    mkdir: int = Query(0),
    upload_url: str = Query(None), content: str = Query(None),
    delete: int = Query(0),
    rename_to: str = Query(None), move_to: str = Query(None),
    filename: str = Query(None),
    help: str = Query(None),
):
    json_mode = json == 1
    help_level, help_hint = resolve_help_level(help) if help else (None, "")

    ops = sum([1 for v in (mkdir == 1, bool(upload_url), content is not None, delete == 1, bool(rename_to), bool(move_to)) if v])
    if ops > 1:
        return error_response("Conflicting parameters: only one operation allowed", 400, json_mode)

    share = find_share_by_vpath("/" + path)
    if not share:
        if json_mode:
            return JSONResponse(status_code=404, content={"success": False, "error": "No share matched", "code": 404})
        raise HTTPException(status_code=404, detail="No share matched")
    err_msg = check_access(share, key)
    if err_msg:
        write_log({"action": "access_denied", "path": path, "ip": request.client.host, "key": mask_key(key), "reason": err_msg})
        if json_mode:
            return JSONResponse(status_code=403, content={"success": False, "error": err_msg, "code": 403})
        raise HTTPException(status_code=403, detail=err_msg)
    abs_path = get_absolute_path(share, "/" + path)

    # 操作分发
    if mkdir == 1:
        return await handle_mkdir(share, abs_path, path, request.client.host, key, json_mode)
    if upload_url:
        fn = filename or os.path.basename(urlparse(upload_url).path) or ""
        return await handle_upload_url(share, abs_path, upload_url, fn, path, request.client.host, key, json_mode)
    if content is not None:
        fn = filename or os.path.basename(path.rstrip("/")) or ""
        return await handle_content_upload(share, abs_path, content, fn, path, request.client.host, key, json_mode)
    if delete == 1:
        return await handle_delete(share, abs_path, path, request.client.host, key, json_mode)
    if rename_to:
        return await handle_rename(share, abs_path, rename_to, path, request.client.host, key, json_mode=json_mode)
    if move_to:
        return await handle_rename(share, abs_path, None, path, request.client.host, key, move_target=move_to, json_mode=json_mode)

    # 常规文件/目录访问
    def _resp(template: str, ctx: dict, status: int = 200):
        if help_level:
            if json_mode:
                ctx["help"] = HELP_MD
                return JSONResponse(content=ctx, status_code=status)
            from fastapi.responses import HTMLResponse
            main_html = templates.get_template(template).render(request=request, **ctx)
            help_html = templates.get_template("help.jinja").render({"level": help_level, "hint": help_hint})
            combined = main_html.replace("</body>", f'<div style="margin:24px auto;max-width:800px"><hr>{help_html}</div></body>')
            return HTMLResponse(combined)
        if json_mode:
            return JSONResponse(content=ctx, status_code=status)
        return templates.TemplateResponse(request, template, ctx)

    log_entry = {"action": "access", "path": path, "ip": request.client.host, "key": mask_key(key), "operation": "unknown"}
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
            if entry.name.startswith("."): continue
            st = entry.stat()
            entry_type = "dir" if entry.is_dir() else "file"
            entries.append({"name": entry.name, "type": entry_type,
                            "size": st.st_size if entry_type == "file" else 0,
                            "modified": datetime.fromtimestamp(st.st_mtime).isoformat()})
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
        if json_mode:
            return _resp(None, {"type": "file", "path": path, "filename": abs_path.name,
                                 "size": file_stat.st_size,
                                 "modified": datetime.fromtimestamp(file_stat.st_mtime).isoformat()})
        content_preview = ""
        is_text = False
        if file_stat.st_size <= settings.preview_max_size:
            try:
                with open(abs_path, "r", encoding="utf-8") as f:
                    content_preview = f.read()
                is_text = True
            except (UnicodeDecodeError, PermissionError, MemoryError): pass
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


@router.put("/s/{path:path}")
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
    err_msg = check_access(share, key)
    if err_msg:
        write_log({"action": "access_denied", "path": path, "ip": request.client.host, "key": mask_key(key), "reason": err_msg})
        return error_response(err_msg, 403, json_mode)
    abs_path = get_absolute_path(share, "/" + path)
    return await handle_put_upload(request, share, abs_path, path, key, json_mode, filename)


@router.post("/s/{path:path}")
async def serve_content_post(
    request: Request, path: str,
    key: str = Query(None), mkdir: int = Query(0),
    json: int = Query(0, alias="json"),
    file: UploadFile = File(None), filename: str = Query(None),
):
    json_mode = json == 1
    if file:
        share = find_share_by_vpath("/" + path)
        if not share:
            return error_response("No share matched", 404, json_mode)
        err_msg = check_access(share, key)
        if err_msg:
            write_log({"action": "access_denied", "path": path, "ip": request.client.host, "key": mask_key(key), "reason": err_msg})
            return error_response(err_msg, 403, json_mode)
        abs_path = get_absolute_path(share, "/" + path)
        return await handle_multipart_upload(request, share, abs_path, path, key, file, json_mode, filename)
    return await serve_content(request=request, path=path, key=key, json=json, mkdir=mkdir)


@router.delete("/s/{path:path}")
async def serve_content_delete(
    request: Request, path: str,
    key: str = Query(None), json: int = Query(0, alias="json"),
):
    return await serve_content(request=request, path=path, key=key, json=json, delete=1)


@router.get("/perm/{path:path}")
async def query_permission(path: str, key: str = Query(...)):
    share = find_share_by_vpath("/" + path)
    if not share:
        return JSONResponse(status_code=404, content={"success": False, "error": "No share matched"})
    err_msg = check_access(share, key)
    if err_msg:
        return JSONResponse(status_code=403, content={"success": False, "error": err_msg})
    return {"path": "/" + path, "share_name": share.name, "permissions": share.permissions}
