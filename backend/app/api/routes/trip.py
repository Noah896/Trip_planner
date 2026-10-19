"""旅行规划API路由"""

import json
from typing import AsyncIterator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from ...agents.trip_planner_agent import get_trip_planner_agent, result_message
from ...models.schemas import (
    TripPlan,
    TripPlanResponse,
    TripRequest,
    UsedMemoryResponse,
)
from ...services.memory_context import (
    apply_long_term_memory,
    save_explicit_preferences,
    used_memory_payload,
)
from ...services.memory_store import get_memory_store

router = APIRouter(prefix="/trip", tags=["旅行规划"])


def _apply_memory(request: TripRequest) -> tuple[TripRequest, list[UsedMemoryResponse]]:
    """读取并注入长期偏好;记忆故障不能阻断正常规划。"""
    try:
        application = apply_long_term_memory(request)
        return application.request, [
            UsedMemoryResponse(**item)
            for item in used_memory_payload(application.used_facts)
        ]
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️  长期偏好读取失败,按无记忆继续: {type(exc).__name__}: {exc}")
        return request, []


def _save_preferences(request: TripRequest, *, degraded: bool) -> None:
    """只把成功规划中的显式偏好写入长期记忆。"""
    if degraded:
        return
    try:
        save_explicit_preferences(request)
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️  长期偏好写入失败: {type(exc).__name__}: {exc}")


def _save_memory(
    request: TripRequest, plan: TripPlan, *, degraded: bool
) -> tuple[bool, int | None]:
    """把正常的表单规划写入会话记忆;失败不影响已经生成好的结果。"""
    if not request.session_id or degraded:
        return False, None
    try:
        saved = get_memory_store().save_successful_plan(
            session_id=request.session_id,
            user_id=request.user_id,
            request=request,
            plan=plan,
            last_message=request.free_text_input or "",
            reset_base=True,
        )
        return True, saved.version
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️  表单规划写入会话记忆失败: {type(exc).__name__}: {exc}")
        return False, None


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

        planning_request, used_memory = _apply_memory(request)

        # 获取Agent实例
        print("🔄 获取多智能体系统实例...")
        agent = get_trip_planner_agent()

        # 生成旅行计划
        print("🚀 开始生成旅行计划...")
        outcome = await agent.plan_trip_with_status(planning_request)

        if outcome.degraded:
            print(f"⚠️  返回降级占位行程: {outcome.reason}\n")
        else:
            print("✅ 旅行计划生成成功,准备返回响应\n")

        memory_saved, memory_version = _save_memory(
            request, outcome.plan, degraded=outcome.degraded
        )

        _save_preferences(request, degraded=outcome.degraded)

        return TripPlanResponse(
            success=True,
            degraded=outcome.degraded,
            message=result_message(outcome.degraded),
            data=outcome.plan,
            session_id=request.session_id,
            memory_saved=memory_saved,
            memory_version=memory_version,
            used_memory=used_memory,
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

    planning_request, used_memory = _apply_memory(request)

    print(f"\n📡 收到流式规划请求: {request.city} {request.start_date}~{request.end_date}")

    async def event_source() -> AsyncIterator[str]:
        try:
            async for event in agent.plan_trip_stream(planning_request):
                if event.get("type") == "result" and event.get("data"):
                    plan = TripPlan.model_validate(event["data"])
                    memory_saved, memory_version = _save_memory(
                        request,
                        plan,
                        degraded=bool(event.get("degraded", False)),
                    )
                    _save_preferences(
                        request, degraded=bool(event.get("degraded", False))
                    )
                    event["memory_saved"] = memory_saved
                    event["memory_version"] = memory_version
                if event.get("type") == "result":
                    event["used_memory"] = [
                        item.model_dump(mode="json") for item in used_memory
                    ]
                    event["used_history"] = None
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
