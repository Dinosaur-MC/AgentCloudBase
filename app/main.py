"""AI Agent FTP 服务 — 入口"""

import time
from typing import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.utils import write_log
from app.routes import admin, files, tools, misc

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


# ------------------- 请求日志中间件 -------------------
@app.middleware("http")
async def log_requests(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
):
    start = time.time()
    response = await call_next(request)
    duration_ms = int((time.time() - start) * 1000)
    path = request.url.path
    if not (
        request.method == "GET"
        and (path.startswith("/admin") or path.startswith("/.well-known/") or path == "/favicon.ico")
    ):
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


# ------------------- 静态文件 -------------------
app.mount("/static", StaticFiles(directory="static"), name="static")

# ------------------- 注册路由 -------------------
app.include_router(admin.router)
app.include_router(files.router)
app.include_router(tools.router)
app.include_router(misc.router)
