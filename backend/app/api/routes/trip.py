"""旅行规划API路由"""

from fastapi import APIRouter, HTTPException

from ...agents.trip_planner_agent import get_trip_planner_agent
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
            message=(
                "AI 规划未完成,已返回占位行程(仅供参考,请稍后重试)"
                if outcome.degraded
                else "旅行计划生成成功"
            ),
            data=outcome.plan,
        )

    except Exception as e:
        print(f"❌ 生成旅行计划失败: {str(e)}")
        import traceback

        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"生成旅行计划失败: {str(e)}")


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
