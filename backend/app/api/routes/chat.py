"""对话式规划路由

和 `/api/trip/plan/stream` 的关系:后者是"表单直通",前者是"聊一句"。
两者最终跑的是**同一条**检索 + 规划流水线,区别只在入口:

- 表单页:把填好的 `TripRequest` 直接放进 `trip` 字段 → 跳过意图识别;
- 聊天页:只给 `message`(可带 `history`)→ 先判定意图、抽槽位,够了就规划。
"""

import json
from typing import AsyncIterator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from ...models.schemas import ChatRequest
from ...services import chat_service

router = APIRouter(prefix="/chat", tags=["对话式规划"])


@router.post(
    "/stream",
    summary="对话式规划(SSE)",
    description=(
        "用一句话开始规划:后端判断意图、抽取条件,信息够了就直接生成行程,"
        "不够就追问。整个过程以 SSE 推进度与结果。\n\n"
        "事件类型(每条都是 `data: <json>`,以空行分隔):\n"
        '- `{"type":"message","text":"..."}` —— 助手回复的**增量**'
        "(同一句话会被拆成很多条,客户端按顺序追加)\n"
        '- `{"type":"progress","stage":...,"status":...}` —— 规划节点进度\n'
        '- `{"type":"artifact","kind":"trip_plan","data":{...}}` —— 结构化行程,前端渲染成卡片\n'
        '- `{"type":"done","request":{...}}` —— 本轮结束\n'
        '- `{"type":"error","message":"..."}` —— 出错(流已开始,只能推事件)\n\n'
        "若客户端已经填好表单,可直接传 `trip` 字段 —— 此时跳过意图识别,"
        "不额外消耗模型调用。"
    ),
)
async def chat_stream(payload: ChatRequest) -> StreamingResponse:
    """对话式规划(流式)。"""
    preview = (payload.message or "").strip()[:40]
    print(f"\n💬 收到对话请求: {preview!r}" + ("(结构化直传)" if payload.trip else ""))

    async def event_source() -> AsyncIterator[str]:
        try:
            # 注意:这里经模块访问(而不是 `from ... import run_chat`),
            # 否则测试 monkeypatch 打不中 —— 模块级导入会固化函数引用。
            async for event in chat_service.run_chat(payload):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as exc:  # noqa: BLE001 - 流已开始,只能把错误推给前端
            print(f"❌ 对话式规划中断: {type(exc).__name__}: {exc}")
            payload_out = {"type": "error", "message": f"对话式规划失败: {exc}"}
            yield f"data: {json.dumps(payload_out, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
