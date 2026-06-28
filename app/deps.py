"""共享 FastAPI 依赖项"""

from fastapi import Request, HTTPException, Depends
from app.utils import get_admin_from_cookie


async def require_admin(request: Request):
    """依赖项：要求管理员登录，未登录则 401"""
    admin = get_admin_from_cookie(request)
    if not admin:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return admin
