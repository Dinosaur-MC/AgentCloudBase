"""共享 FastAPI 依赖项"""

from fastapi import Request, HTTPException, Query, Depends
from app.utils import (
    get_admin_from_cookie,
    find_share_by_vpath,
    check_access,
    get_absolute_path,
    write_log,
    mask_key,
    ShareConfig,
)


async def require_admin(request: Request):
    """依赖项：要求管理员登录，未登录则 401"""
    admin = get_admin_from_cookie(request)
    if not admin:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return admin


async def require_share(path: str) -> ShareConfig:
    """依赖项：解析共享路径，不存在则 404"""
    share = find_share_by_vpath("/" + path)
    if not share:
        raise HTTPException(status_code=404, detail="No share matched")
    return share


def _get_path_params(request: Request) -> list[str]:
    return [str(v) for v in request.path_params.values()]


async def verify_access(
    request: Request,
    share: ShareConfig = Depends(require_share),
    key: str = Query(""),
    json: int = Query(0, alias="json"),
):
    """依赖项：验证密钥 + 共享状态，通过后返回 (share, abs_path, json_mode)"""
    err_msg = check_access(share, key)
    if err_msg:
        paths = _get_path_params(request)
        path = paths[0] if paths else ""
        write_log(
            {
                "action": "access_denied",
                "path": path,
                "ip": request.client.host,
                "key": mask_key(key),
                "reason": err_msg,
            }
        )
        raise HTTPException(status_code=403, detail=err_msg)
    json_mode = json == 1
    paths = _get_path_params(request)
    path = paths[0] if paths else ""
    abs_path = get_absolute_path(share, "/" + path)
    return share, abs_path, json_mode
