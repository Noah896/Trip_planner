#!/usr/bin/env python
"""手动验证 ``/api/trip/plan/stream``:确认进度事件是**边跑边到**的。

这个脚本解决一个只看代码看不出来的问题:SSE 很容易被"攒到最后一起发"
(反代缓冲、框架内部把生成器跑完再返回、忘了 flush)。这里给每条事件打上
到达时间,如果所有事件的 ``+Xs`` 都挤在最后一秒,那就是被缓冲了。

需要后端已经在 8000 端口跑起来(见 AGENTS.md 第 1 节的启动姿势)::

    & 'E:\\hello agents_demo\\.venv\\Scripts\\python.exe' -X utf8 scripts\\check_stream.py

**会真实调用一次大模型**,请先确认额度。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import httpx

from auth_client import get_auth_headers

BACKEND_URL = os.getenv("TRIP_BACKEND_URL", "http://127.0.0.1:8000")
STREAM_PATH = "/api/trip/plan/stream"

PAYLOAD = {
    "city": "北京",
    "start_date": "2025-06-01",
    "end_date": "2025-06-02",
    "travel_days": 2,
    "transportation": "公共交通",
    "accommodation": "经济型酒店",
    "preferences": ["历史文化"],
    "free_text_input": "",
}


async def main() -> int:
    started = time.monotonic()
    events = 0
    arrivals = []

    print(f"POST {BACKEND_URL}{STREAM_PATH}")
    async with httpx.AsyncClient(timeout=300.0) as client:
        try:
            headers = await get_auth_headers(client, BACKEND_URL)
        except RuntimeError as exc:
            print(f"❌ {exc}")
            return 1
        async with client.stream(
            "POST",
            f"{BACKEND_URL}{STREAM_PATH}",
            json=PAYLOAD,
            headers=headers,
        ) as response:
            print(
                f"HTTP {response.status_code}  "
                f"content-type={response.headers.get('content-type')}"
            )
            if response.status_code != 200:
                print(await response.aread())
                return 1

            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue

                payload = json.loads(line[len("data:") :].strip())
                delta = time.monotonic() - started
                arrivals.append(delta)
                events += 1

                if payload["type"] == "progress":
                    print(
                        f"  +{delta:6.1f}s  进度  {payload['label']:<6}"
                        f"{payload['status']:<7}(后端自计 {payload['elapsed']}s)"
                    )
                elif payload["type"] == "result":
                    days = len(payload["data"]["days"])
                    print(
                        f"  +{delta:6.1f}s  结果  degraded={payload['degraded']} "
                        f"days={days} elapsed={payload['elapsed']}s"
                    )
                    print(f"        {payload['message']}")
                else:
                    print(f"  +{delta:6.1f}s  错误  {payload['message']}")
                    return 1

    if events < 2:
        print("\n⚠️  只收到一条事件,无法判断是否真的流式。")
        return 1

    spread = arrivals[-1] - arrivals[0]
    print(f"\n共 {events} 条事件;首条 +{arrivals[0]:.1f}s,末条 +{arrivals[-1]:.1f}s")
    if spread < 1.0:
        print("⚠️  所有事件几乎同时到达 —— 说明被缓冲了,不是真流式。")
        return 1
    print(f"✅ 事件分散在 {spread:.1f} 秒里,是真流式。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
