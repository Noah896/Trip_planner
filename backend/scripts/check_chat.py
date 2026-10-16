#!/usr/bin/env python
"""手动验证 ``/api/chat/stream``:一句话 → 追问 → 补信息 → 出行程。

这个脚本模拟真实的两轮对话,专门验证聊天入口最关键的一环
——**槽位补全**:第一轮只给"北京三天"(缺日期),后端应该追问而不是硬跑;
第二轮补上时间后,才真正开始规划并把行程作为 artifact 推回来。

**会真实调用模型**:每轮 1 次意图识别,真正规划那轮再多 1 次(约 3 次)。

需要后端已在 8000 端口跑起来(见 AGENTS.md 第 1 节)::

    & 'E:\\hello agents_demo\\.venv\\Scripts\\python.exe' -X utf8 scripts\\check_chat.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

import httpx

BACKEND_URL = os.getenv("TRIP_BACKEND_URL", "http://127.0.0.1:8000")
CHAT_PATH = "/api/chat/stream"

FIRST_MESSAGE = "我想去北京玩三天"
SECOND_MESSAGE = "下周末出发,带一个六岁的小孩"
# 第一句话里说的天数 —— 用来回归检查"模型别自己算日期"
EXPECTED_DAYS = 3


async def send(
    client: httpx.AsyncClient, message: str, history: List[Dict[str, str]]
) -> Optional[Dict[str, Any]]:
    """发一轮,打印事件,返回最后那个 artifact(没有就返回 None)。"""
    started = time.monotonic()
    artifact: Optional[Dict[str, Any]] = None

    print(f"\n{'=' * 68}\n👤 用户:{message}\n{'=' * 68}")
    body = {"message": message, "history": history}

    async with client.stream("POST", f"{BACKEND_URL}{CHAT_PATH}", json=body) as response:
        print(f"HTTP {response.status_code}  content-type={response.headers.get('content-type')}")
        if response.status_code != 200:
            print(await response.aread())
            return None

        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue

            event = json.loads(line[len("data:") :].strip())
            delta = time.monotonic() - started
            kind = event.get("type")

            if kind == "message":
                print(f"  +{delta:6.1f}s  🤖 {event['text']}")
            elif kind == "progress":
                print(
                    f"  +{delta:6.1f}s  ⏳ {event['label']} {event['status']}"
                    f"(后端自计 {event['elapsed']}s)"
                )
            elif kind == "artifact":
                artifact = event
                plan = event.get("data") or {}
                days = plan.get("days") or []
                weather = plan.get("weather_info") or []
                print(
                    f"  +{delta:6.1f}s  📦 行程卡片 kind={event['kind']} "
                    f"degraded={event['degraded']} 城市={plan.get('city')} "
                    f"天数={len(days)} 天气条目={len(weather)}"
                )
                for day in days:
                    names = "、".join(a.get("name", "") for a in day.get("attractions", []))
                    print(f"        第{day.get('day_index', 0) + 1}天 {day.get('date')}: {names}")
                if event.get("request"):
                    print(f"        解析出的条件: {json.dumps(event['request'], ensure_ascii=False)}")
            elif kind == "error":
                print(f"  +{delta:6.1f}s  ❌ {event['message']}")
                return None
            elif kind == "done":
                print(f"  +{delta:6.1f}s  ✔ 本轮结束")

    return artifact


async def main() -> int:
    history: List[Dict[str, str]] = []
    artifacts = 0

    async with httpx.AsyncClient(timeout=300.0) as client:
        # ---- 第 1 轮:只给城市和天数,期望被追问 ----
        first = await send(client, FIRST_MESSAGE, history)
        if first is not None:
            print("\n⚠️  第一轮就出结果了(预期是先追问日期)。模型行为有波动,不算错,但请留意。")
        history.append({"role": "user", "content": FIRST_MESSAGE})

        # ---- 第 2 轮:补齐时间,期望真的规划 ----
        second = await send(client, SECOND_MESSAGE, history)
        if second is not None:
            artifacts += 1

    print(f"\n{'=' * 68}")
    if not artifacts:
        print("❌ 两轮下来都没拿到行程 artifact,请检查上面的事件流。")
        return 1

    # 回归检查:用户说的是"三天",出来的行程就必须是三天。
    # (踩过一次:模型自己推返程日,把三天算成两天,结果行程少一天。)
    days = len((second.get("data") or {}).get("days") or [])
    if days != EXPECTED_DAYS:
        print(f"❌ 用户说的是 {EXPECTED_DAYS} 天,但行程只有 {days} 天。")
        return 1

    print(f"✅ 聊天链路可用:补全信息后拿到 {days} 天的结构化行程(artifact)。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
