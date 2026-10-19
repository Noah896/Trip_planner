#!/usr/bin/env python
"""用无头 Edge 真跑一遍聊天页(CDP 驱动),而不是只看编译能不能过。

为什么需要它:前端的验证条件比后端弱得多 —— `npm run build` 只能证明
"类型和模板能编译",证明不了"页面能挂载、能发消息、能把行程卡片渲染出来"。
这个脚本通过 Chrome DevTools Protocol 真的开一个浏览器、真的在输入框里打字、
真的点发送,然后把渲染结果读回来。

前置:
  1. 后端跑在 8000(`& python run.py`);
  2. 前端 dev server 跑在 5173(`npm run dev`);
  3. 无头 Edge 带 `--remote-debugging-port=9222` 启动。

**会真实调用模型**(约 2~3 次),请先确认额度::

    & 'E:\\hello agents_demo\\.venv\\Scripts\\python.exe' -X utf8 scripts\\check_chat_ui.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any, Optional

import aiohttp

CDP_BASE = os.getenv("CDP_BASE", "http://127.0.0.1:9222")
PAGE_URL = os.getenv("CHAT_PAGE_URL", "http://localhost:5173/chat")
BACKEND_URL = os.getenv("TRIP_BACKEND_URL", "http://127.0.0.1:8000")
MESSAGE = "北京玩三天,下周末出发,带一个六岁小孩"
PLAN_TIMEOUT = 180.0


class Cdp:
    """极简的 CDP 客户端(只需要 send / evaluate 两个能力)。"""

    def __init__(self, ws: aiohttp.ClientWebSocketResponse):
        self._ws = ws
        self._id = 0

    async def send(self, method: str, params: Optional[dict] = None) -> dict:
        self._id += 1
        message_id = self._id
        await self._ws.send_json(
            {"id": message_id, "method": method, "params": params or {}}
        )
        while True:
            msg = await self._ws.receive_json()
            if msg.get("id") == message_id:
                return msg

    async def eval(self, expression: str) -> Any:
        result = await self.send(
            "Runtime.evaluate",
            {"expression": expression, "awaitPromise": True, "returnByValue": True},
        )
        payload = result.get("result", {}).get("result", {})
        return payload.get("value")


async def open_page(session: aiohttp.ClientSession) -> str:
    """新建一个标签页并返回它的 WebSocket 调试地址。"""
    async with session.put(f"{CDP_BASE}/json/new?{PAGE_URL}") as response:
        target = await response.json()
    return target["webSocketDebuggerUrl"]


async def browser_auth_script(session: aiohttp.ClientSession) -> str:
    """返回在页面加载前写入 Bearer 登录态的 JS。"""
    token = os.getenv("TRIP_AUTH_TOKEN", "").strip()
    if not token:
        username = os.getenv("TRIP_AUTH_USERNAME", "").strip()
        password = os.getenv("TRIP_AUTH_PASSWORD", "")
        if username and password:
            async with session.post(
                f"{BACKEND_URL}/api/auth/login",
                json={"username": username, "password": password},
            ) as response:
                if response.status != 200:
                    raise RuntimeError(
                        f"脚本登录失败({response.status}): {await response.text()}"
                    )
                token = (await response.json())["access_token"]

    if not token:
        async with session.get(f"{BACKEND_URL}/api/auth/status") as response:
            status = await response.json()
        if not status.get("enabled", True):
            return ""
        raise RuntimeError(
            "后端已开启鉴权。请设置 TRIP_AUTH_TOKEN,"
            "或同时设置 TRIP_AUTH_USERNAME / TRIP_AUTH_PASSWORD。"
        )

    async with session.get(
        f"{BACKEND_URL}/api/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    ) as response:
        if response.status != 200:
            raise RuntimeError(
                f"读取当前用户失败({response.status}): {await response.text()}"
            )
        user = await response.json()

    user_json = json.dumps(user, ensure_ascii=False)
    return f"""
        localStorage.setItem('tripPlannerAuthToken', {json.dumps(token)});
        localStorage.setItem('tripPlannerAuthUser', {json.dumps(user_json)});
        localStorage.removeItem('tripPlannerAuthExpiresAt');
        localStorage.removeItem('tripPlannerAuthDisabled');
    """


async def main() -> int:
    async with aiohttp.ClientSession() as session:
        try:
            auth_script = await browser_auth_script(session)
        except RuntimeError as exc:
            print(f"❌ {exc}")
            return 1
        ws_url = await open_page(session)
        async with session.ws_connect(ws_url, max_msg_size=0) as ws:
            cdp = Cdp(ws)
            await cdp.send("Page.enable")
            await cdp.send("Runtime.enable")
            if auth_script:
                await cdp.send(
                    "Page.addScriptToEvaluateOnNewDocument",
                    {"source": auth_script},
                )

            # ---------- 1) 页面能不能挂载 ----------
            await cdp.send("Page.navigate", {"url": PAGE_URL})
            for _ in range(20):
                await asyncio.sleep(0.5)
                mounted = await cdp.eval(
                    "!!document.querySelector('.chat-input textarea')"
                )
                if mounted:
                    break
            else:
                print("❌ 聊天页没挂载起来(找不到输入框)。")
                error = await cdp.eval(
                    "JSON.stringify({body: document.body.innerText.slice(0,400)})"
                )
                print(error)
                return 1

            state = json.loads(
                await cdp.eval(
                    """JSON.stringify({
                        path: location.pathname,
                        title: (document.querySelector('.chat-title')||{}).innerText || '',
                        starters: [...document.querySelectorAll('.starter')].map(e => e.innerText),
                        hasInput: !!document.querySelector('.chat-input textarea')
                    })"""
                )
            )
            print(f"✅ 页面挂载成功: {state['path']}  {state['title']}")
            print(f"   引导词: {state['starters']}")

            # ---------- 2) 打字 + 点发送 ----------
            await cdp.eval(
                f"""(() => {{
                    const ta = document.querySelector('.chat-input textarea');
                    const setter = Object.getOwnPropertyDescriptor(
                        window.HTMLTextAreaElement.prototype, 'value'
                    ).set;
                    setter.call(ta, {json.dumps(MESSAGE, ensure_ascii=False)});
                    ta.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    document.querySelector('.chat-input button').click();
                    return true;
                }})()"""
            )
            print(f"\n📤 已发送: {MESSAGE}")

            # ---------- 3) 等行程卡片出现 ----------
            waited = 0.0
            while waited < PLAN_TIMEOUT:
                await asyncio.sleep(3)
                waited += 3
                snapshot = json.loads(
                    await cdp.eval(
                        """JSON.stringify({
                            hasPlan: !!document.querySelector('.trip-plan-view'),
                            failed: !!document.querySelector('.bot-failed'),
                            botText: [...document.querySelectorAll('.bot-text')].map(e => e.innerText),
                            progress: [...document.querySelectorAll('.progress-row')].map(e => e.innerText.replace(/\\n/g,' '))
                        })"""
                    )
                )
                if snapshot["hasPlan"] or snapshot["failed"]:
                    break
                print(f"   +{waited:5.0f}s 等待中… {snapshot['progress']}")

            print()
            for line in snapshot["progress"]:
                print(f"   进度: {line}")
            for text in snapshot["botText"]:
                print(f"   🤖 {text}")

            if snapshot["failed"]:
                print("\n❌ 前端收到了失败标记(见上面 .bot-failed)。")
                return 1
            if not snapshot["hasPlan"]:
                print(f"\n❌ {PLAN_TIMEOUT:.0f} 秒内没渲染出行程卡片。")
                return 1

            # ---------- 4) 卡片里真的有数据吗 ----------
            card = json.loads(
                await cdp.eval(
                    """JSON.stringify({
                        title: (document.querySelector('.plan-title')||{}).innerText || '',
                        dates: (document.querySelector('.plan-dates')||{}).innerText || '',
                        total: (document.querySelector('.plan-total')||{}).innerText || '',
                        days: [...document.querySelectorAll('.trip-plan-view .day')].map(d => d.innerText.replace(/\\n/g,' | ')),
                        weather: [...document.querySelectorAll('.weather-chip')].map(e => e.innerText),
                        hasResultLink: !!document.querySelector('.artifact-actions button')
                    })"""
                )
            )
            print(f"\n📦 渲染出的行程卡片:")
            print(f"   标题: {card['title']}")
            print(f"   日期: {card['dates']}    {card['total']}")
            print(f"   天气: {card['weather']}")
            for index, day in enumerate(card["days"], 1):
                print(f"   第{index}天: {day}")
            print(f"   有「查看完整行程」按钮: {card['hasResultLink']}")

            if not card["days"]:
                print("\n❌ 卡片渲染出来了,但里面没有任何一天的行程。")
                return 1

            print("\n✅ 聊天页可用:输入 → 事件流 → 行程卡片渲染,全链路在浏览器里跑通了。")
            return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
