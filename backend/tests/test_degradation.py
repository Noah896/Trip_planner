"""降级(兜底占位行程)逻辑测试

对应早期规划清单里的 P1 项:"修掉'假数据当成功'"。
这里不初始化 LLM,直接复用 ``MultiAgentTripPlanner.__new__`` 或子类,
因此可以在无 API Key 的环境下跑通。
"""

import asyncio

from app.agents.trip_planner_agent import MultiAgentTripPlanner, PlanOutcome
from app.models.schemas import TripPlan, TripRequest


def _request() -> TripRequest:
    return TripRequest(
        city="北京",
        start_date="2025-06-01",
        end_date="2025-06-02",
        travel_days=2,
        transportation="公共交通",
        accommodation="经济型酒店",
    )


def _bare_planner() -> MultiAgentTripPlanner:
    """不调用 __init__(避免初始化 LLM)的规划器实例。"""
    return MultiAgentTripPlanner.__new__(MultiAgentTripPlanner)


class _EmptyGraph:
    """三路检索全部返回空的图。"""

    async def ainvoke(self, state, config=None):
        return {}


class _BrokenGraph:
    async def ainvoke(self, state, config=None):
        raise RuntimeError("模型服务不可用")


class _HangingGraph:
    """永远不返回的图,用于验证总时长上限会把它掐掉并降级。"""

    async def ainvoke(self, state, config=None):
        await asyncio.sleep(3600)
        return {}


class _GraphPlanner(MultiAgentTripPlanner):
    """替换掉建图过程,用假图驱动 plan_trip_with_status。"""

    def __init__(self, graph):
        self._graph = graph

    async def _ensure_graph(self):
        return self._graph


class TestFallbackPlan:
    def test_placeholder_content_is_explicitly_labelled(self):
        plan = _bare_planner()._create_fallback_plan(_request())

        assert isinstance(plan, TripPlan)
        # 占位内容本身没有变(前端仍需要一个能渲染的结构)
        assert plan.days[0].attractions[0].name == "北京景点1"
        # 但必须在正文里说明这是占位结果
        assert "占位行程" in plan.overall_suggestions
        assert plan.overall_suggestions.startswith("⚠️")


class TestParseResponse:
    def test_returns_none_when_no_json_found(self):
        assert _bare_planner()._parse_response("模型只回了一段话", _request()) is None

    def test_returns_none_on_invalid_payload(self):
        assert _bare_planner()._parse_response('{"days": "不是数组"}', _request()) is None

    def test_returns_plan_for_valid_payload(self):
        payload = """
        ```json
        {
          "city": "北京",
          "start_date": "2025-06-01",
          "end_date": "2025-06-02",
          "days": [],
          "weather_info": [],
          "overall_suggestions": "注意防晒"
        }
        ```
        """
        plan = _bare_planner()._parse_response(payload, _request())
        assert isinstance(plan, TripPlan)
        assert plan.city == "北京"


class TestDegradedSignalling:
    def test_empty_search_results_are_marked_degraded(self):
        planner = _GraphPlanner(_EmptyGraph())

        outcome = asyncio.run(planner.plan_trip_with_status(_request()))

        assert outcome.degraded is True
        assert "三路检索均无结果" in outcome.reason
        assert isinstance(outcome.plan, TripPlan)

    def test_graph_exception_is_marked_degraded(self):
        planner = _GraphPlanner(_BrokenGraph())

        outcome = asyncio.run(planner.plan_trip_with_status(_request()))

        assert outcome.degraded is True
        assert "规划过程出错" in outcome.reason

    def test_total_timeout_is_marked_degraded(self, monkeypatch):
        """总时长上限必须兜住"图一直不返回"的情况(LLM_TIMEOUT 管不到总时长)。"""
        from app.config import get_settings

        monkeypatch.setattr(get_settings(), "plan_total_timeout", 1)
        planner = _GraphPlanner(_HangingGraph())

        outcome = asyncio.run(planner.plan_trip_with_status(_request()))

        assert outcome.degraded is True
        assert "规划超时" in outcome.reason

    def test_degrade_keeps_plan_and_reason(self):
        outcome = _bare_planner()._degrade(_request(), "测试原因")

        assert isinstance(outcome, PlanOutcome)
        assert outcome.degraded is True
        assert outcome.reason == "测试原因"
        assert isinstance(outcome.plan, TripPlan)

    def test_plan_trip_keeps_backward_compatible_return_type(self):
        """旧契约 plan_trip(request) -> TripPlan 不能被破坏。"""

        class _StubPlanner(MultiAgentTripPlanner):
            def __init__(self):
                pass  # 不初始化 LLM

            async def plan_trip_with_status(self, request):
                return PlanOutcome(
                    plan=MultiAgentTripPlanner._create_fallback_plan(
                        _bare_planner(), request
                    ),
                    degraded=True,
                    reason="stub",
                )

        plan = asyncio.run(_StubPlanner().plan_trip(_request()))

        assert isinstance(plan, TripPlan)
        assert plan.days[0].attractions[0].name == "北京景点1"
