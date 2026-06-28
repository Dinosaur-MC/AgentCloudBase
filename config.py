"""AI Agent FTP 服务配置 — 使用 pydantic-settings 管理"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ------------------- 服务配置 --------------------
    listen_host: str = "0.0.0.0"
    listen_port: int = 8000
    debug: bool = False
    
    # ------------------- 管理员配置 -------------------
    admin_key: str = "admin123"
    secret_key: str = "supersecretkey"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60
    config_file: str = "config.json"

    # ------------------- 日志配置 -------------------
    log_dir: str = "logs"
    log_filename_prefix: str = "access"
    log_file_ext: str = ".jsonl"
    log_max_size: int = 10 * 1024 * 1024      # 10 MB
    log_max_age_days: int = 90

    # ------------------- 上传/安全配置 -------------------
    max_upload_size: int = 500 * 1024 * 1024    # 500 MB
    max_content_param_size: int = 64 * 1024     # 64 KB
    upload_url_timeout: int = 30                # 连接超时秒数
    upload_url_download_timeout: int = 60       # 下载超时秒数
    upload_url_max_redirects: int = 5
    min_disk_free: int = 100 * 1024 * 1024      # 100 MB
    write_rate_limit: int = 60                  # 每分钟写操作上限
    preview_max_size: int = 512 * 1024          # 预览最大文件大小 (512 KB)

    model_config = {"env_prefix": ""}


settings = Settings()
