"""验证脚本共用的 Bearer 登录辅助。"""

from __future__ import annotations

import os
from typing import Dict

import httpx


async def get_auth_headers(
    client: httpx.AsyncClient, backend_url: str
) -> Dict[str, str]:
    """优先使用 TRIP_AUTH_TOKEN;未开启鉴权时返回空头。"""
    token = os.getenv("TRIP_AUTH_TOKEN", "").strip()
    if token:
        return {"Authorization": f"Bearer {token}"}

    username = os.getenv("TRIP_AUTH_USERNAME", "").strip()
    password = os.getenv("TRIP_AUTH_PASSWORD", "")
    if username and password:
        response = await client.post(
            f"{backend_url}/api/auth/login",
            json={"username": username, "password": password},
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"脚本登录失败({response.status_code}): {response.text}"
            )
        return {
            "Authorization": f"Bearer {response.json()['access_token']}"
        }

    status = await client.get(f"{backend_url}/api/auth/status")
    if status.status_code == 200 and not status.json().get("enabled", True):
        return {}
    raise RuntimeError(
        "后端已开启鉴权。请设置 TRIP_AUTH_TOKEN,"
        "或同时设置 TRIP_AUTH_USERNAME / TRIP_AUTH_PASSWORD。"
    )
