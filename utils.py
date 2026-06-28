"""AI Agent FTP 服务 — 工具函数与数据模型"""

import os
import json
import time
import hashlib
import re
import ipaddress
import socket
import shutil
import unicodedata
from datetime import datetime, timedelta
from typing import List, Optional
from pathlib import Path
from collections import defaultdict
from urllib.parse import urlparse

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from jose import JWTError, jwt

from config import settings


# ==================== 数据模型 ====================

class ShareConfig(BaseModel):
    id: str
    name: str
    virtual_path: str
    real_path: str
    permissions: dict = {"list": True, "read": True, "write": False, "delete": False, "rename": False}
    access_key: str


# ==================== 配置读写 ====================

def load_config() -> List[ShareConfig]:
    if not os.path.exists(settings.config_file):
        return []
    with open(settings.config_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    return [ShareConfig(**item) for item in data]


def save_config(configs: List[ShareConfig]):
    with open(settings.config_file, "w", encoding="utf-8") as f:
        json.dump([cfg.model_dump() for cfg in configs], f, indent=2, ensure_ascii=False)


# ==================== 日志分片工具 ====================

def _ensure_log_dir():
    os.makedirs(settings.log_dir, exist_ok=True)


def _list_log_shards() -> List[Path]:
    _ensure_log_dir()
    return sorted(Path(settings.log_dir).glob(f"{settings.log_filename_prefix}*{settings.log_file_ext}"))


def _cleanup_old_logs():
    shards = _list_log_shards()
    cutoff = datetime.now().timestamp() - settings.log_max_age_days * 86400
    current = _current_shard_path()
    for shard in shards:
        if shard.stat().st_mtime < cutoff and shard != current:
            shard.unlink(missing_ok=True)


_cached_shard: Path | None = None


def _current_shard_path() -> Path:
    global _cached_shard
    if _cached_shard is not None and _cached_shard.exists() and _cached_shard.stat().st_size < settings.log_max_size:
        return _cached_shard
    _ensure_log_dir()
    shards = _list_log_shards()
    if not shards:
        _cached_shard = Path(settings.log_dir) / f"{settings.log_filename_prefix}{settings.log_file_ext}"
    else:
        latest = shards[-1]
        if latest.stat().st_size < settings.log_max_size:
            _cached_shard = latest
        else:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            _cached_shard = Path(settings.log_dir) / f"{settings.log_filename_prefix}_{ts}{settings.log_file_ext}"
    return _cached_shard


_counter_cleanup = 0


def _counter_for_cleanup() -> bool:
    global _counter_cleanup
    _counter_cleanup = (_counter_cleanup + 1) % 100
    return _counter_cleanup == 0


def write_log(entry: dict):
    entry["timestamp"] = datetime.now().isoformat()
    log_path = _current_shard_path()
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    if _counter_for_cleanup():
        _cleanup_old_logs()


def iter_all_logs() -> List[dict]:
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
    logs = iter_all_logs()
    return list(reversed(logs))[offset: offset + limit]


def compute_stats() -> dict:
    configs = load_config()
    shards = _list_log_shards()
    total_logs = total_size = 0
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


def count_resource_views(path: str) -> int:
    target = path.lstrip("/")
    return sum(1 for entry in iter_all_logs()
               if entry.get("action") == "access" and entry.get("path") == target)


# ==================== 速率限制器 ====================

class RateLimiter:
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


write_rate_limiter = RateLimiter(settings.write_rate_limit)


# ==================== 安全校验工具 ====================

def validate_filename(name: str) -> str:
    if not name or name in (".", ".."):
        raise HTTPException(status_code=400, detail="Invalid filename")
    if "/" in name or "\\" in name:
        raise HTTPException(status_code=400, detail="Filename cannot contain path separators")
    if "\0" in name:
        raise HTTPException(status_code=400, detail="Invalid filename")
    cleaned = re.sub(r'[<>:"|?*]', "_", name)
    return unicodedata.normalize("NFC", cleaned)


def validate_upload_url(url: str) -> str:
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


def check_disk_space(path: str, min_free: int = None):
    if min_free is None:
        min_free = settings.min_disk_free
    usage = shutil.disk_usage(os.path.dirname(path) or ".")
    if usage.free < min_free:
        raise HTTPException(status_code=507, detail="Insufficient storage space")


def mask_key(key: str) -> str:
    return (mask_key(key)) if key else "None"


# ==================== JSON 响应工具 ====================

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


# ==================== 共享匹配与鉴权 ====================

def find_share_by_vpath(virtual_path: str) -> Optional[ShareConfig]:
    configs = load_config()
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
    relative = request_path
    cfg_vpath_raw = share.virtual_path.rstrip("/")
    cfg_vpath = cfg_vpath_raw + "/"
    if request_path.rstrip("/") == cfg_vpath_raw:
        relative = ""
    elif request_path.startswith(cfg_vpath):
        relative = request_path[len(cfg_vpath):]
    safe_relative = os.path.normpath(relative).lstrip("/")
    real_root = os.path.realpath(share.real_path)
    real_path_str = os.path.realpath(os.path.join(real_root, safe_relative))
    # Windows: 统一大小写和分隔符后再比较，避免 E: vs e: 或 / vs \ 导致误判
    root_norm = os.path.normcase(os.path.normpath(real_root))
    path_norm = os.path.normcase(os.path.normpath(real_path_str))
    if not path_norm.startswith(root_norm + os.sep) and path_norm != root_norm:
        raise HTTPException(status_code=403, detail="Access denied")
    return Path(real_path_str)


# ==================== 写操作安全检查链 ====================

def check_share_permission(share: ShareConfig, perm: str) -> bool:
    return share.permissions.get(perm, False)


def require_write_permission(share: ShareConfig, path: str, ip: str, key: str, json_mode: bool = False):
    if not check_share_permission(share, "write"):
        write_log({"action": "upload_denied", "path": path, "ip": ip, "key": mask_key(key), "reason": "permission_denied"})
        return error_response("Write permission denied", 403, json_mode)
    if not write_rate_limiter.check(ip):
        write_log({"action": "upload_denied", "path": path, "ip": ip, "key": mask_key(key), "reason": "rate_limited"})
        return error_response("Rate limit exceeded. Try again later.", 429, json_mode)
    return None


def require_delete_permission(share: ShareConfig, path: str, ip: str, key: str, json_mode: bool = False):
    if not check_share_permission(share, "delete"):
        write_log({"action": "delete_denied", "path": path, "ip": ip, "key": mask_key(key), "reason": "permission_denied"})
        return error_response("Delete permission denied", 403, json_mode)
    return None


def require_rename_permission(share: ShareConfig, path: str, ip: str, key: str, json_mode: bool = False):
    if not check_share_permission(share, "rename"):
        write_log({"action": "rename_denied", "path": path, "ip": ip, "key": mask_key(key), "reason": "permission_denied"})
        return error_response("Rename permission denied", 403, json_mode)
    return None


# ==================== 文件操作处理函数 ====================

async def handle_mkdir(share, abs_path, path, ip, key, json_mode=False):
    err = require_write_permission(share, path, ip, key, json_mode)
    if err:
        return err
    if abs_path.exists():
        return error_response("Path already exists", 409, json_mode)
    try:
        abs_path.mkdir(parents=True, exist_ok=True)
        write_log({"action": "dir_created", "path": path, "ip": ip, "key": mask_key(key)})
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
        async with httpx.AsyncClient(timeout=httpx.Timeout(settings.upload_url_timeout, connect=settings.upload_url_timeout)) as client:
            async with client.stream("GET", url, max_redirects=settings.upload_url_max_redirects) as resp:
                if resp.status_code not in (200, 206):
                    temp_path.unlink(missing_ok=True)
                    return error_response(f"Upload URL returned status {resp.status_code}", 400, json_mode)
                size = 0
                with open(temp_path, "wb") as f:
                    async for chunk in resp.aiter_bytes():
                        size += len(chunk)
                        if size > settings.max_upload_size:
                            temp_path.unlink(missing_ok=True)
                            return error_response("File too large", 413, json_mode)
                        f.write(chunk)
        os.rename(temp_path, final_path)
        rel = str(final_path.relative_to(Path(share.real_path).resolve()))
        write_log({"action": "file_uploaded", "path": rel, "ip": ip, "key": mask_key(key), "source": "upload_url", "size": size})
        return success_response(f"File uploaded: {final_path.name}", {"name": final_path.name, "size": size}, json_mode)
    except httpx.RequestError as e:
        temp_path.unlink(missing_ok=True)
        return error_response(f"Failed to download from URL: {str(e)}", 502, json_mode)


async def handle_content_upload(share, abs_path, content_data, filename, path, ip, key, json_mode=False):
    err = require_write_permission(share, path, ip, key, json_mode)
    if err:
        return err
    try:
        encoded = content_data.encode("utf-8")
    except UnicodeEncodeError:
        return error_response("Invalid UTF-8 content", 400, json_mode)
    if len(encoded) > settings.max_content_param_size:
        return error_response("Content too large (max 64KB)", 413, json_mode)
    if "\0" in encoded:
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
    size = len(encoded)
    rel = str(final_path.relative_to(Path(share.real_path).resolve()))
    write_log({"action": "file_uploaded", "path": rel, "ip": ip, "key": mask_key(key), "source": "content_param", "size": size})
    return success_response(f"File uploaded: {final_path.name}", {"name": final_path.name, "size": size}, json_mode)


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
        write_log({"action": "file_deleted", "path": path, "type": entry_type, "ip": ip, "key": mask_key(key)})
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
        write_log({"action": "file_renamed", "path": path, "dest": rel, "ip": ip, "key": mask_key(key)})
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
    if len(body) > settings.max_upload_size:
        return error_response("File too large (max 500MB)", 413, json_mode)
    temp_path = abs_path.with_suffix(abs_path.suffix + ".tmp")
    with open(temp_path, "wb") as f:
        f.write(body)
    os.rename(temp_path, abs_path)
    write_log({"action": "file_uploaded", "path": path, "ip": request.client.host, "key": mask_key(key), "source": "put", "size": len(body)})
    return success_response(f"File uploaded: {abs_path.name}", {"name": abs_path.name, "size": len(body)}, json_mode)


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
            if size > settings.max_upload_size:
                temp_path.unlink(missing_ok=True)
                return error_response("File too large (max 500MB)", 413, json_mode)
            f.write(chunk)
    os.rename(temp_path, final_path)
    write_log({"action": "file_uploaded", "path": path, "ip": request.client.host, "key": mask_key(key), "source": "multipart", "size": size})
    return success_response(f"File uploaded: {final_path.name}", {"name": final_path.name, "size": size}, json_mode)


# ==================== JWT 工具 ====================

def create_jwt_token(data: dict) -> str:
    expire = datetime.utcnow() + timedelta(minutes=settings.access_token_expire_minutes)
    to_encode = data.copy()
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.secret_key, algorithm=settings.jwt_algorithm)


def get_admin_from_cookie(request: Request) -> Optional[str]:
    token = request.cookies.get("admin_token")
    if not token:
        return None
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.jwt_algorithm])
        return payload.get("sub")
    except JWTError:
        return None
