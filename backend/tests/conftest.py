"""pytest 公共配置

让 ``pytest`` 无论从哪个目录启动,都能 ``import app.*``。
"""

import sys
from pathlib import Path

import pytest

# backend/ 目录(即 app 包的父目录)
BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


@pytest.fixture(autouse=True)
def _disable_auth_by_default(monkeypatch):
    """旧功能测试默认不构造 Bearer token;鉴权专项测试会显式覆盖为 True。"""
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", False)
    # 没有登录用户时不预占模型配额,避免旧用例写入真实 data/ 数据库。
    monkeypatch.setattr(settings, "model_quota_enabled", False)
    # RAG 用例会显式开启并注入临时索引;其它测试默认不读写 data/ 下的知识库。
    monkeypatch.setattr(settings, "rag_enabled", False)
