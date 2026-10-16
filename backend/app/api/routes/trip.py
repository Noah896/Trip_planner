"""旅行规划API路由"""

import json
from typing import AsyncIterator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from ...agents.trip_planner_agent import get_trip_planner_agent, result_message
from ...models.schemas import TripPlanResponse, TripRequest

router = APIRouter(prefix="/trip", tags=["旅行规划"])


@router.post(
    "/plan",
    response_model=TripPlanResponse,
    summary="生成旅行计划",
    description="根据用户输入的旅行需求,生成详细的旅行计划",
)
async def plan_trip(request: TripRequest):
    """
    生成旅行计划

    Args:
        request: 旅行请求参数

    Returns:
        旅行计划响应
    """
    try:
        print(f"\n{'=' * 60}")
        print("📥 收到旅行规划请求:")
        print(f"   城市: {request.city}")
        print(f"   日期: {request.start_date} - {request.end_date}")
        print(f"   天数: {request.travel_days}")
        print(f"{'=' * 60}\n")

        # 获取Agent实例
        print("🔄 获取多智能体系统实例...")
        agent = get_trip_planner_agent()

        # 生成旅行计划
        print("🚀 开始生成旅行计划...")
        outcome = await agent.plan_trip_with_status(request)

        if outcome.degraded:
            print(f"⚠️  返回降级占位行程: {outcome.reason}\n")
        else:
            print("✅ 旅行计划生成成功,准备返回响应\n")

        return TripPlanResponse(
            success=True,
            degraded=outcome.degraded,
            message=result_message(outcome.degraded),
            data=outcome.plan,
        )

    except Exception as e:
        print(f"❌ 生成旅行计划失败: {str(e)}")
        import traceback

        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"生成旅行计划失败: {str(e)}")


@router.post(
    "/plan/stream",
    summary="生成旅行计划(流式进度)",
    description=(
        "返回的最终内容与 `/api/trip/plan` 完全一致,区别是**过程中就会推送进度**:"
        "每完成一步(景点检索 / 天气查询 / 酒店检索 / 生成行程)推一条 SSE 事件,"
        "前端据此显示真实进度,而不是盯着进度条干等一分钟。\n\n"
        "事件形如 `data: {json}`,每条以空行分隔:progress 事件带 "
        "`stage`/`label`/`status`/`elapsed`,最后一条是 result(结构同 TripPlanResponse)。"
    ),
)
async def plan_trip_stream(request: TripRequest) -> StreamingResponse:
    """生成旅行计划,并以 SSE 持续推送进度。"""
    agent = get_trip_planner_agent()

    print(f"\n📡 收到流式规划请求: {request.city} {request.start_date}~{request.end_date}")

    async def event_source() -> AsyncIterator[str]:
        try:
            async for event in agent.plan_trip_stream(request):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as exc:  # noqa: BLE001 - 流已经开始,只能把错误推给前端
            print(f"❌ 流式规划中断: {type(exc).__name__}: {exc}")
            payload = {"type": "error", "message": f"生成旅行计划失败: {exc}"}
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # 关掉反代缓冲,否则进度会被攒到最后一起发出来
            "X-Accel-Buffering": "no",
        },
    )


@router.get(
    "/health",
    summary="健康检查",
    description="检查旅行规划服务是否正常",
)
async def health_check():
    """健康检查

    注意:这里只报告运行时状态,不会触发高德 MCP 工具加载
    (工具与 LangGraph 在首次调用 /plan 时才惰性构建)。
    """
    try:
        agent = get_trip_planner_agent()

        return {
            "status": "healthy",
            "service": "trip-planner",
            **agent.describe(),
        }
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"服务不可用: {str(e)}")
