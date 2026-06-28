import os
import json
import time
import hashlib
from datetime import datetime, timedelta
from typing import List, Optional
from pathlib import Path
import re
import ipaddress
import socket
import shutil
from collections import defaultdict
from urllib.parse import urlparse

from fastapi import (
    FastAPI,
    Request,
    Response,
    HTTPException,
    Query,
    Cookie,
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
from pydantic import BaseModel
from jose import JWTError, jwt
import uvicorn

# ------------------- 配置 -------------------
ADMIN_KEY = os.getenv("ADMIN_KEY", "admin123")
SECRET_KEY = os.getenv("SECRET_KEY", "supersecretkey")
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60
CONFIG_FILE = os.getenv("CONFIG_FILE", "config.json")

# ------------------- 日志配置 -------------------
LOG_DIR = "logs"                     # 日志存放目录
LOG_FILENAME_PREFIX = "access"       # 日志文件名前缀
LOG_FILE_EXT = ".jsonl"              # 日志文件扩展名 (JSON Lines)
LOG_MAX_SIZE = 10 * 1024 * 1024      # 每个日志分片的最大字节数 (10 MB)
LOG_MAX_AGE_DAYS = 90                # 日志保留天数 (超过自动清理)
MAX_UPLOAD_SIZE = 500 * 1024 * 1024  # 单文件上传上限 (500 MB)
MAX_CONTENT_PARAM_SIZE = 64 * 1024   # content= 参数上限 (64 KB)
UPLOAD_URL_TIMEOUT = 30              # URL 拉取连接超时
UPLOAD_URL_DOWNLOAD_TIMEOUT = 60     # URL 拉取下载超时
UPLOAD_URL_MAX_REDIRECTS = 5
MIN_DISK_FREE = 100 * 1024 * 1024    # 最小剩余磁盘空间 (100 MB)
WRITE_RATE_LIMIT = 60                # 每分钟写操作上限


# ------------------- 数据模型 -------------------
class ShareConfig(BaseModel):
    id: str  # 唯一ID
    name: str  # 显示名称
    virtual_path: str  # 虚拟路径，如 /data
    real_path: str  # 服务器真实绝对路径
    permissions: dict = {"list": True, "read": True, "write": False, "delete": False, "rename": False}
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


# ------------------- 速率限制器 -------------------
class RateLimiter:
    """简易滑动窗口速率限制器"""
    def __init__(self, max_requests: int, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._requests: dict[str, list[float]] = defaultdict(list)

    def check(self, key: str) -> bool:
        now = time.time()
        cutoff = now - self.window_seconds
        self._requests[key] = [t for t in self._requests[key] if t > cutoff]
        if len(self._requests[key]) >= self.max_requests:
            return False
        self._requests[key].append(now)
        return True


# ------------------- 安全校验工具 -------------------
def validate_filename(name: str) -> str:
    """净化文件名，移除危险字符"""
    if not name or name in (".", ".."):
        raise HTTPException(status_code=400, detail="Invalid filename")
    if "/" in name or "\\" in name:
        raise HTTPException(status_code=400, detail="Filename cannot contain path separators")
    if "\0" in name:
        raise HTTPException(status_code=400, detail="Invalid filename")
    cleaned = re.sub(r'[<>:"|?*]', "_", name)
    import unicodedata
    return unicodedata.normalize("NFC", cleaned)


def validate_upload_url(url: str) -> str:
    """验证上传 URL 安全性（SSRF 防护）"""
    parsed = urlparse(url)
    if parsed.scheme not in ("https",):
        raise HTTPException(status_code=400, detail="Only HTTPS URLs are allowed")
    try:
        host = socket.gethostbyname(parsed.hostname)
        addr = ipaddress.ip_address(host)
        if addr.is_private or addr.is_loopback or addr.is_link_local:
            raise HTTPException(status_code=400, detail="URL points to a private/internal address")
    except socket.gaierror:
        raise HTTPException(status_code=400, detail="Could not resolve upload URL host")
    return url


def check_disk_space(path: str, min_free: int = MIN_DISK_FREE):
    """检查磁盘剩余空间"""
    usage = shutil.disk_usage(os.path.dirname(path) or ".")
    if usage.free < min_free:
        raise HTTPException(status_code=507, detail="Insufficient storage space")


write_rate_limiter = RateLimiter(WRITE_RATE_LIMIT)


# ------------------- JSON 响应工具 -------------------
def json_response(data: dict, status_code: int = 200):
    return JSONResponse(content=data, status_code=status_code)


def error_response(message: str, code: int = 400, json_mode: bool = False):
    if json_mode:
        return JSONResponse(content={"success": False, "error": message, "code": code}, status_code=code)
    raise HTTPException(status_code=code, detail=message)


def success_response(message: str = "", data: dict = None, json_mode: bool = False):
    result = {"success": True}
    if message:
        result["message"] = message
    if data:
        result["data"] = data
    if json_mode:
        return JSONResponse(content=result)
    return result


# ------------------- 写操作安全检查链 -------------------
def check_share_permission(share: ShareConfig, perm: str) -> bool:
    return share.permissions.get(perm, False)


def require_write_permission(share: ShareConfig, path: str, ip: str, key: str, json_mode: bool = False):
    if not check_share_permission(share, "write"):
        write_log({"action": "upload_denied", "path": path, "ip": ip, "key": key[:4] + "***", "reason": "permission_denied"})
        return error_response("Write permission denied", 403, json_mode)
    if not write_rate_limiter.check(ip):
        write_log({"action": "upload_denied", "path": path, "ip": ip, "key": key[:4] + "***", "reason": "rate_limited"})
        return error_response("Rate limit exceeded. Try again later.", 429, json_mode)
    return None


def require_delete_permission(share: ShareConfig, path: str, ip: str, key: str, json_mode: bool = False):
    if not check_share_permission(share, "delete"):
        write_log({"action": "delete_denied", "path": path, "ip": ip, "key": key[:4] + "***", "reason": "permission_denied"})
        return error_response("Delete permission denied", 403, json_mode)
    return None


def require_rename_permission(share: ShareConfig, path: str, ip: str, key: str, json_mode: bool = False):
    if not check_share_permission(share, "rename"):
        write_log({"action": "rename_denied", "path": path, "ip": ip, "key": key[:4] + "***", "reason": "permission_denied"})
        return error_response("Rename permission denied", 403, json_mode)
    return None


# ------------------- 文件操作处理函数 -------------------
async def handle_mkdir(share, abs_path, path, ip, key, json_mode=False):
    err = require_write_permission(share, path, ip, key, json_mode)
    if err:
        return err
    if abs_path.exists():
        return error_response("Path already exists", 409, json_mode)
    try:
        abs_path.mkdir(parents=True, exist_ok=True)
        write_log({"action": "dir_created", "path": path, "ip": ip, "key": key[:4] + "***"})
        return success_response(f"Directory created: {path}", json_mode=json_mode)
    except (OSError, PermissionError) as e:
        return error_response(str(e), 500, json_mode)


async def handle_upload_url(share, abs_path, url, filename, path, ip, key, json_mode=False):
    err = require_write_permission(share, path, ip, key, json_mode)
    if err:
        return err
    validate_upload_url(url)
    if not filename:
        filename = os.path.basename(urlparse(url).path) or "download"
    filename = validate_filename(filename)
    target = abs_path if abs_path.is_dir() else abs_path.parent
    final_path = target / filename
    check_disk_space(str(final_path))
    import httpx
    temp_path = final_path.with_suffix(final_path.suffix + ".tmp")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(UPLOAD_URL_TIMEOUT, connect=UPLOAD_URL_TIMEOUT)) as client:
            async with client.stream("GET", url, max_redirects=UPLOAD_URL_MAX_REDIRECTS) as resp:
                if resp.status_code not in (200, 206):
                    temp_path.unlink(missing_ok=True)
                    return error_response(f"Upload URL returned status {resp.status_code}", 400, json_mode)
                size = 0
                with open(temp_path, "wb") as f:
                    async for chunk in resp.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_UPLOAD_SIZE:
                            temp_path.unlink(missing_ok=True)
                            return error_response("File too large", 413, json_mode)
                        f.write(chunk)
        os.rename(temp_path, final_path)
        write_log({"action": "file_uploaded", "path": str(final_path.relative_to(Path(share.real_path).resolve())), "ip": ip, "key": key[:4] + "***", "source": "upload_url", "size": size})
        d = {"name": final_path.name, "size": size}
        return success_response(f"File uploaded: {final_path.name}", d, json_mode)
    except httpx.RequestError as e:
        temp_path.unlink(missing_ok=True)
        return error_response(f"Failed to download from URL: {str(e)}", 502, json_mode)


async def handle_content_upload(share, abs_path, content_data, filename, path, ip, key, json_mode=False):
    err = require_write_permission(share, path, ip, key, json_mode)
    if err:
        return err
    if len(content_data.encode("utf-8")) > MAX_CONTENT_PARAM_SIZE:
        return error_response("Content too large (max 64KB)", 413, json_mode)
    try:
        content_data.encode("utf-8")
    except UnicodeEncodeError:
        return error_response("Invalid UTF-8 content", 400, json_mode)
    if "\0" in content_data:
        return error_response("Binary content not allowed via content parameter", 400, json_mode)
    if not filename:
        filename = os.path.basename(path.rstrip("/")) or "untitled.txt"
    filename = validate_filename(filename)
    target = abs_path if abs_path.is_dir() else abs_path.parent
    final_path = target / filename
    check_disk_space(str(final_path))
    temp_path = final_path.with_suffix(final_path.suffix + ".tmp")
    temp_path.write_text(content_data, encoding="utf-8")
    os.rename(temp_path, final_path)
    size = len(content_data.encode("utf-8"))
    write_log({"action": "file_uploaded", "path": str(final_path.relative_to(Path(share.real_path).resolve())), "ip": ip, "key": key[:4] + "***", "source": "content_param", "size": size})
    d = {"name": final_path.name, "size": size}
    return success_response(f"File uploaded: {final_path.name}", d, json_mode)


async def handle_delete(share, abs_path, path, ip, key, json_mode=False):
    err = require_delete_permission(share, path, ip, key, json_mode)
    if err:
        return err
    if not abs_path.exists():
        return error_response("Path not found", 404, json_mode)
    real_root = os.path.realpath(share.real_path)
    if str(abs_path.resolve()) == real_root:
        return error_response("Cannot delete share root directory", 403, json_mode)
    if abs_path.is_dir():
        try:
            if any(abs_path.iterdir()):
                return error_response("Directory not empty. Delete its contents first.", 409, json_mode)
        except (OSError, PermissionError) as e:
            return error_response(str(e), 500, json_mode)
    try:
        if abs_path.is_dir():
            abs_path.rmdir()
        else:
            abs_path.unlink()
        entry_type = "directory" if abs_path.is_dir() else "file"
        write_log({"action": "file_deleted", "path": path, "type": entry_type, "ip": ip, "key": key[:4] + "***"})
        return success_response(f"Deleted: {path}", json_mode=json_mode)
    except (OSError, PermissionError) as e:
        return error_response(str(e), 500, json_mode)


async def handle_rename(share, abs_path, new_name, path, ip, key, move_target=None, json_mode=False):
    err = require_rename_permission(share, path, ip, key, json_mode)
    if err:
        return err
    if not abs_path.exists():
        return error_response("Path not found", 404, json_mode)
    if move_target:
        target_share = find_share_by_vpath(move_target)
        if target_share is None or target_share.id != share.id:
            return error_response("Cannot move across shares", 403, json_mode)
        target_rel = move_target.lstrip("/")
        target_abs = get_absolute_path(share, "/" + target_rel)
        final_path = target_abs
    else:
        new_name_clean = validate_filename(new_name)
        if not new_name_clean:
            return error_response("Invalid target name", 400, json_mode)
        final_path = abs_path.parent / new_name_clean
    if final_path.exists():
        return error_response("Target already exists", 409, json_mode)
    real_root = os.path.realpath(share.real_path)
    if not str(final_path.resolve()).startswith(real_root + os.sep) and str(final_path.resolve()) != real_root:
        return error_response("Target path outside share root", 403, json_mode)
    try:
        abs_path.rename(final_path)
        rel = str(final_path.relative_to(Path(share.real_path).resolve()))
        write_log({"action": "file_renamed", "path": path, "dest": rel, "ip": ip, "key": key[:4] + "***"})
        return success_response(f"Renamed to: {final_path.name}", {"from": path, "to": rel}, json_mode)
    except (OSError, PermissionError) as e:
        return error_response(str(e), 500, json_mode)


async def handle_put_upload(request, share, abs_path, path, key, json_mode, filename=None):
    err = require_write_permission(share, path, request.client.host, key, json_mode)
    if err:
        return err
    if abs_path.is_dir() and filename:
        abs_path = abs_path / validate_filename(filename)
    elif abs_path.is_dir() and not filename:
        return error_response("Filename required when uploading to a directory", 400, json_mode)
    if not abs_path.parent.exists():
        return error_response("Parent directory does not exist", 404, json_mode)
    check_disk_space(str(abs_path))
    body = await request.body()
    if len(body) > MAX_UPLOAD_SIZE:
        return error_response("File too large (max 500MB)", 413, json_mode)
    temp_path = abs_path.with_suffix(abs_path.suffix + ".tmp")
    with open(temp_path, "wb") as f:
        f.write(body)
    os.rename(temp_path, abs_path)
    write_log({"action": "file_uploaded", "path": path, "ip": request.client.host, "key": key[:4] + "***", "source": "put", "size": len(body)})
    d = {"name": abs_path.name, "size": len(body)}
    return success_response(f"File uploaded: {abs_path.name}", d, json_mode)


async def handle_multipart_upload(request, share, abs_path, path, key, file, json_mode=False):
    err = require_write_permission(share, path, request.client.host, key, json_mode)
    if err:
        return err
    filename = validate_filename(file.filename or "upload")
    if abs_path.is_dir():
        final_path = abs_path / filename
    else:
        final_path = abs_path.parent / filename
    check_disk_space(str(final_path))
    temp_path = final_path.with_suffix(final_path.suffix + ".tmp")
    size = 0
    with open(temp_path, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_SIZE:
                temp_path.unlink(missing_ok=True)
                return error_response("File too large (max 500MB)", 413, json_mode)
            f.write(chunk)
    os.rename(temp_path, final_path)
    write_log({"action": "file_uploaded", "path": path, "ip": request.client.host, "key": key[:4] + "***", "source": "multipart", "size": size})
    d = {"name": final_path.name, "size": size}
    return success_response(f"File uploaded: {final_path.name}", d, json_mode)


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
    write_perm: bool = Form(False),
    delete_perm: bool = Form(False),
    rename_perm: bool = Form(False),
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

    # 过滤
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
    logs = list(reversed(all_logs))[offset : offset + per_page]
    total_pages = max(1, (total_logs + per_page - 1) // per_page)

    return templates.TemplateResponse(
        request,
        "admin_logs.html",
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


# ------------------- 服务内容 -------------------
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

    # 找到匹配的共享
    share = find_share_by_vpath("/" + path)
    if not share:
        if json_mode:
            return JSONResponse(status_code=404, content={"success": False, "error": "No share matched", "code": 404})
        raise HTTPException(status_code=404, detail="No share matched")
    if not validate_access_key(share, key):
        write_log({"action": "access_denied", "path": path, "ip": request.client.host, "key": key[:4] + "***" if key else "None"})
        if json_mode:
            return JSONResponse(status_code=403, content={"success": False, "error": "Invalid access key", "code": 403})
        raise HTTPException(status_code=403, detail="Invalid access key")

    # 安全取真实路径
    abs_path = get_absolute_path(share, "/" + path)

    # 操作分发
    if mkdir == 1:
        return await handle_mkdir(share, abs_path, path, request.client.host, key, json_mode)
    if upload_url:
        filename = os.path.basename(path.rstrip("/")) or ""
        return await handle_upload_url(share, abs_path, upload_url, filename, path, request.client.host, key, json_mode)
    if content is not None:
        filename = os.path.basename(path.rstrip("/")) or ""
        return await handle_content_upload(share, abs_path, content, filename, path, request.client.host, key, json_mode)
    if delete == 1:
        return await handle_delete(share, abs_path, path, request.client.host, key, json_mode)
    if rename_to:
        return await handle_rename(share, abs_path, rename_to, path, request.client.host, key, json_mode=json_mode)
    if move_to:
        return await handle_rename(share, abs_path, None, path, request.client.host, key, move_target=move_to, json_mode=json_mode)

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
        if json_mode:
            return JSONResponse(status_code=404, content={"success": False, "error": "File or directory not found", "code": 404})
        raise HTTPException(status_code=404, detail="File or directory not found")

    if abs_path.is_dir():
        # 目录请求
        if not share.permissions.get("list", False):
            write_log({**log_entry, "operation": "list_denied", "status": 403})
            if json_mode:
                return JSONResponse(status_code=403, content={"success": False, "error": "Listing not allowed", "code": 403})
            raise HTTPException(status_code=403, detail="Listing not allowed")
        # 收集目录内容
        entries = []
        for entry in sorted(abs_path.iterdir()):
            if entry.name.startswith("."):
                continue
            entry_type = "dir" if entry.is_dir() else "file"
            entries.append({
                "name": entry.name,
                "type": entry_type,
                "size": entry.stat().st_size if entry_type == "file" else 0,
                "modified": datetime.fromtimestamp(entry.stat().st_mtime).isoformat(),
            })
        log_entry["operation"] = "list_dir"
        write_log(log_entry)
        if json_mode:
            return JSONResponse(content={
                "type": "directory", "path": path,
                "entries": entries, "share_name": share.name,
            })
        return templates.TemplateResponse(
            request, "listing.html", {
                "path": path, "entries": entries, "key": key,
                "share_name": share.name,
                "can_write": share.permissions.get("write", False),
                "can_delete": share.permissions.get("delete", False),
                "can_rename": share.permissions.get("rename", False),
            },
        )
    else:
        # 文件请求
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
                content_txt = abs_path.read_text(encoding="utf-8")
                return PlainTextResponse(content_txt)
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
                "content": content_preview if is_text else None,
                "is_text": is_text,
            })

        view_count = count_resource_views("/" + path)
        return templates.TemplateResponse(
            request, "file_display.html", {
                "path": path, "filename": abs_path.name,
                "size": file_stat.st_size,
                "modified": datetime.fromtimestamp(file_stat.st_mtime).isoformat(),
                "content": content_preview, "is_text": is_text,
                "key": key, "view_count": view_count,
                "help_url": "/help?level=basic",
                "can_write": share.permissions.get("write", False),
                "can_delete": share.permissions.get("delete", False),
                "can_rename": share.permissions.get("rename", False),
            },
        )


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
        write_log({"action": "access_denied", "path": path, "ip": request.client.host, "key": key[:4] + "***" if key else "None"})
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
            write_log({"action": "access_denied", "path": path, "ip": request.client.host, "key": key[:4] + "***" if key else "None"})
            return error_response("Invalid access key", 403, json_mode)
        abs_path = get_absolute_path(share, "/" + path)
        return await handle_multipart_upload(request, share, abs_path, path, key, file, json_mode)
    return await serve_content(
        request=request, path=path, key=key,
        raw=0, download=0, json=json, mkdir=mkdir,
        upload_url=None, content=None, delete=0,
        rename_to=None, move_to=None,
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
        upload_url=None, content=None, delete=1,
        rename_to=None, move_to=None,
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
