"""启动脚本"""

import os

import uvicorn
from app.config import get_settings

if __name__ == "__main__":
    settings = get_settings()

    # 热重载只在开发环境开启(生产用 `APP_ENV=production`,或
    # 直接用 `uvicorn app.api.main:app --workers N`)。
    reload_enabled = settings.is_development and os.getenv("UVICORN_RELOAD", "1") != "0"

    uvicorn.run(
        "app.api.main:app",
        host=settings.host,
        port=settings.port,
        reload=reload_enabled,
        log_level=settings.log_level.lower()
    )

