import os
import json
import time
import hashlib
from datetime import datetime, timedelta
from typing import List, Optional
from pathlib import Path

from fastapi import (
    FastAPI,
    Request,
    Response,
    HTTPException,
    Query,
    Cookie,
    Form,
    status,
)
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    FileResponse,
    PlainTextResponse,
)
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from jose import JWTError, jwt
import uvicorn

# ------------------- 配置 -------------------
ADMIN_KEY = os.getenv("ADMIN_KEY", "admin123")
SECRET_KEY = os.getenv("SECRET_KEY", "supersecretkey")
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60
CONFIG_FILE = "config.json"

# ------------------- 日志配置 -------------------
LOG_DIR = "logs"                     # 日志存放目录
LOG_FILENAME_PREFIX = "access"       # 日志文件名前缀
LOG_FILE_EXT = ".jsonl"              # 日志文件扩展名 (JSON Lines)
LOG_MAX_SIZE = 10 * 1024 * 1024      # 每个日志分片的最大字节数 (10 MB)
LOG_MAX_AGE_DAYS = 90                # 日志保留天数 (超过自动清理)


# ------------------- 数据模型 -------------------
class ShareConfig(BaseModel):
    id: str  # 唯一ID
    name: str  # 显示名称
    virtual_path: str  # 虚拟路径，如 /data
    real_path: str  # 服务器真实绝对路径
    permissions: dict = {"list": True, "read": True}  # 权限：list / read
    access_key: str  # 此共享的访问密钥


# ------------------- 工具函数 -------------------
def load_config() -> List[ShareConfig]:
    if not os.path.exists(CONFIG_FILE):
        return []
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return [ShareConfig(**item) for item in data]


def save_config(configs: List[ShareConfig]):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump([cfg.model_dump() for cfg in configs], f, indent=2, ensure_ascii=False)


# ------------------- 日志分片工具 -------------------

def _ensure_log_dir():
    """确保日志目录存在"""
    os.makedirs(LOG_DIR, exist_ok=True)


def _list_log_shards() -> List[Path]:
    """返回所有日志分片文件，按修改时间升序"""
    _ensure_log_dir()
    return sorted(Path(LOG_DIR).glob(f"{LOG_FILENAME_PREFIX}*{LOG_FILE_EXT}"))


def _cleanup_old_logs():
    """清理超过保留天数的日志分片"""
    shards = _list_log_shards()
    cutoff = datetime.now().timestamp() - LOG_MAX_AGE_DAYS * 86400
    for shard in shards:
        if shard.stat().st_mtime < cutoff and shard != _current_shard_path():
            # 跳过当前正在写入的分片
            shard.unlink(missing_ok=True)


def _current_shard_path() -> Path:
    """获取当前应写入的日志分片路径（自动分片）"""
    _ensure_log_dir()
    shards = _list_log_shards()

    if not shards:
        # 首个分片：logs/access.jsonl
        return Path(LOG_DIR) / f"{LOG_FILENAME_PREFIX}{LOG_FILE_EXT}"

    latest = shards[-1]
    # 若最新分片未超限，继续使用
    if latest.stat().st_size < LOG_MAX_SIZE:
        return latest

    # 超限 → 创建新分片，文件名带时间戳
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path(LOG_DIR) / f"{LOG_FILENAME_PREFIX}_{ts}{LOG_FILE_EXT}"


def write_log(entry: dict):
    """写入一条日志（JSONL），自动处理分片"""
    entry["timestamp"] = datetime.now().isoformat()
    log_path = _current_shard_path()
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    # 懒清理：每写 100 条左右清理一次过期分片（粗略计数）
    if _counter_for_cleanup():
        _cleanup_old_logs()


_counter_cleanup = 0


def _counter_for_cleanup() -> bool:
    global _counter_cleanup
    _counter_cleanup = (_counter_cleanup + 1) % 100
    return _counter_cleanup == 0


def _iter_all_logs() -> List[dict]:
    """从所有分片中读取全部日志，按时间升序（最新分片在末尾）"""
    logs: List[dict] = []
    for shard in _list_log_shards():
        try:
            with open(shard, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        logs.append(json.loads(line))
        except (FileNotFoundError, json.JSONDecodeError):
            continue
    return logs


def read_logs(limit: int = 100, offset: int = 0) -> List[dict]:
    """读取日志，支持分片，最新在前"""
    logs = _iter_all_logs()
    return list(reversed(logs))[offset : offset + limit]


def count_resource_views(path: str) -> int:
    """统计某个资源被访问的次数（跨所有分片）"""
    count = 0
    for shard in _list_log_shards():
        try:
            with open(shard, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        if entry.get("action") == "access" and entry.get("path") == path.lstrip("/"):
                            count += 1
                    except json.JSONDecodeError:
                        continue
        except FileNotFoundError:
            continue
    return count


def find_share_by_vpath(virtual_path: str) -> Optional[ShareConfig]:
    """根据请求虚拟路径匹配最具体的共享配置"""
    configs = load_config()
    # 保证虚拟路径以 / 结尾
    vpath = (
        virtual_path.rstrip("/") + "/"
        if not virtual_path.endswith("/")
        else virtual_path
    )
    best_match = None
    best_len = 0
    for cfg in configs:
        cfg_vpath = cfg.virtual_path.rstrip("/") + "/"
        if vpath.startswith(cfg_vpath) and len(cfg_vpath) > best_len:
            best_match = cfg
            best_len = len(cfg_vpath)
    return best_match


def validate_access_key(share: ShareConfig, key: Optional[str]) -> bool:
    if not key:
        return False
    return share.access_key == key


def get_absolute_path(share: ShareConfig, request_path: str) -> Path:
    """将虚拟路径转换为真实绝对路径，并做安全检查"""
    # 请求路径是相对 share.virtual_path 之后的剩余部分
    relative = request_path
    cfg_vpath = share.virtual_path.rstrip("/") + "/"
    if request_path.startswith(cfg_vpath):
        relative = request_path[len(cfg_vpath) :]
    # 净化路径
    safe_relative = os.path.normpath(relative).lstrip("/")
    # 组合真实根路径
    real_root = os.path.realpath(share.real_path)
    real_path = os.path.realpath(os.path.join(real_root, safe_relative))
    # 防止目录穿越
    if not real_path.startswith(real_root + os.sep) and real_path != real_root:
        raise HTTPException(status_code=403, detail="Access denied")
    return Path(real_path)


def create_jwt_token(data: dict) -> str:
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode = data.copy()
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=JWT_ALGORITHM)


def get_admin_from_cookie(request: Request) -> Optional[str]:
    token = request.cookies.get("admin_token")
    if not token:
        return None
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[JWT_ALGORITHM])
        return payload.get("sub")
    except JWTError:
        return None


# ------------------- FastAPI 应用 -------------------
app = FastAPI(title="AI Agent FTP Service", version="1.0")
TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ------------------- 管理页面 -------------------
@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    admin = get_admin_from_cookie(request)
    if not admin:
        return templates.TemplateResponse(request, "admin_login.html", {"error": None})
    configs = load_config()
    return templates.TemplateResponse(
        request, "admin_dashboard.html", {"shares": configs}
    )


@app.post("/admin/login")
async def admin_login(request: Request, key: str = Form(...)):
    if key != ADMIN_KEY:
        return templates.TemplateResponse(
            request, "admin_login.html", {"error": "Invalid admin key"}
        )
    token = create_jwt_token({"sub": "admin"})
    resp = RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)
    resp.set_cookie(
        "admin_token", token, httponly=True, max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60
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
    access_key: str = Form(...),
):
    admin = get_admin_from_cookie(request)
    if not admin:
        raise HTTPException(status_code=401, detail="Unauthorized")
    configs = load_config()
    # 简单ID生成
    new_id = hashlib.md5(f"{name}{virtual_path}{time.time()}".encode()).hexdigest()[:8]
    new_share = ShareConfig(
        id=new_id,
        name=name,
        virtual_path=virtual_path,
        real_path=os.path.realpath(real_path),
        permissions={"list": list_perm, "read": read_perm},
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


@app.get("/admin/logs", response_class=HTMLResponse)
async def view_logs(request: Request, page: int = 1, per_page: int = 50):
    admin = get_admin_from_cookie(request)
    if not admin:
        raise HTTPException(status_code=401, detail="Unauthorized")
    all_logs = _iter_all_logs()
    total_logs = len(all_logs)
    offset = (page - 1) * per_page
    logs = list(reversed(all_logs))[offset : offset + per_page]
    total_pages = (total_logs + per_page - 1) // per_page
    return templates.TemplateResponse(
        request,
        "admin_logs.html",
        {
            "logs": logs,
            "page": page,
            "total_pages": total_pages,
        },
    )


# ------------------- 服务内容 -------------------
@app.get("/s/{path:path}")
async def serve_content(
    request: Request,
    path: str,
    key: str = Query(None),
    raw: int = Query(0),
    download: int = Query(0),
):
    # 找到匹配的共享
    share = find_share_by_vpath("/" + path)
    if not share:
        raise HTTPException(status_code=404, detail="No share matched")
    if not validate_access_key(share, key):
        write_log(
            {
                "action": "access_denied",
                "path": path,
                "ip": request.client.host,
                "key": key[:4] + "***" if key else "None",
            }
        )
        raise HTTPException(status_code=403, detail="Invalid access key")

    # 安全取真实路径
    abs_path = get_absolute_path(share, "/" + path)

    # 记录日志
    log_entry = {
        "action": "access",
        "path": path,
        "ip": request.client.host,
        "key": key[:4] + "***",
        "operation": "unknown",
    }

    if not abs_path.exists():
        write_log({**log_entry, "status": 404})
        raise HTTPException(status_code=404, detail="File or directory not found")

    if abs_path.is_dir():
        # 目录请求
        if not share.permissions.get("list", False):
            write_log({**log_entry, "operation": "list_denied", "status": 403})
            raise HTTPException(status_code=403, detail="Listing not allowed")
        # 收集目录内容
        entries = []
        for entry in sorted(abs_path.iterdir()):
            if entry.name.startswith("."):  # 隐藏文件可选忽略
                continue
            entry_type = "dir" if entry.is_dir() else "file"
            entries.append(
                {
                    "name": entry.name,
                    "type": entry_type,
                    "size": entry.stat().st_size if entry_type == "file" else 0,
                    "modified": datetime.fromtimestamp(
                        entry.stat().st_mtime
                    ).isoformat(),
                }
            )
        log_entry["operation"] = "list_dir"
        write_log(log_entry)
        # 生成返回的虚拟路径前缀，用于构造链接
        virtual_dir = "/" + path.rstrip("/") + "/"
        return templates.TemplateResponse(
            request,
            "listing.html",
            {
                "path": path,
                "entries": entries,
                "key": key,
                "share_name": share.name,
            },
        )
    else:
        # 文件请求
        if not share.permissions.get("read", False):
            write_log({**log_entry, "operation": "read_denied", "status": 403})
            raise HTTPException(status_code=403, detail="Read not allowed")

        log_entry["operation"] = "read_file"
        write_log(log_entry)

        # 下载模式：直接返回文件
        if download == 1:
            return FileResponse(abs_path, filename=abs_path.name)

        # 原始内容模式（纯文本）
        if raw == 1:
            try:
                content = abs_path.read_text(encoding="utf-8")
                return PlainTextResponse(content)
            except UnicodeDecodeError:
                # 非文本文件，回退为文件响应
                return FileResponse(abs_path, filename=abs_path.name)

        # 默认 HTML 显示
        file_stat = abs_path.stat()
        content_preview = ""
        is_text = False
        try:
            with open(abs_path, "r", encoding="utf-8") as f:
                content_preview = f.read()
            is_text = True
        except (UnicodeDecodeError, PermissionError):
            pass  # 二进制文件

        view_count = count_resource_views("/" + path)
        return templates.TemplateResponse(
            request,
            "file_display.html",
            {
                "path": path,
                "filename": abs_path.name,
                "size": file_stat.st_size,
                "modified": datetime.fromtimestamp(file_stat.st_mtime).isoformat(),
                "content": content_preview,
                "is_text": is_text,
                "key": key,
                "view_count": view_count,
                "help_url": "/help?level=basic",
            },
        )


# ------------------- 帮助页 -------------------
@app.get("/help", response_class=HTMLResponse)
async def help_page(request: Request, level: str = "basic"):
    return templates.TemplateResponse(
        request,
        "help.html",
        {
            "level": level,
        },
    )


# ------------------- 启动入口 -------------------
if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
