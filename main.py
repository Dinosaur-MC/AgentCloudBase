from app.config import settings
import uvicorn

# ------------------- 启动入口 -------------------
if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=settings.listen_host,
        port=settings.listen_port,
        reload=settings.debug,
    )
