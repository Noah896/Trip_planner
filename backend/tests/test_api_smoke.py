"""API 冒烟测试

用 ``TestClient`` 打桩掉 LLM 与高德工具,只验证 HTTP 契约:
路由是否挂载、请求校验是否生效、响应结构是否符合前端约定。

说明:这里不用 ``with TestClient(app)``,因此不会触发 ``lifespan``
里的配置校验——测试不依赖真实 API Key,也不会发起任何网络请求。
"""

from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from app.agents.trip_planner_agent import PlanOutcome
from app.api.main import app
from app.api.routes import trip as trip_routes
from app.models.schemas import (
    Attraction,
    DayPlan,
    KnowledgeSource,
    Location,
    Meal,
    TripPlan,
)

PLACEHOLDER_CITY = "北京"


def _build_plan(city: str = PLACEHOLDER_CITY, travel_days: int = 2) -> TripPlan:
    """构造一份最小的合法 TripPlan。"""
    days: List[DayPlan] = []
    for i in range(travel_days):
        days.append(
            DayPlan(
                date=f"2025-06-0{i + 1}",
                day_index=i,
                description=f"第{i + 1}天行程",
                transportation="公共交通",
                accommodation="经济型酒店",
                attractions=[
                    Attraction(
                        name=f"{city}景点1",
                        address=f"{city}市",
                        location=Location(longitude=116.4, latitude=39.9),
                        visit_duration=120,
                        description="描述",
                    )
                ],
                meals=[
                    Meal(type="breakfast", name="早餐", description="早餐"),
                    Meal(type="lunch", name="午餐", description="午餐"),
                    Meal(type="dinner", name="晚餐", description="晚餐"),
                ],
            )
        )
    return TripPlan(
        city=city,
        start_date="2025-06-01",
        end_date="2025-06-02",
        days=days,
        weather_info=[],
        overall_suggestions="建议提前查看开放时间。",
    )


class _FakeAgent:
    """替身 Agent:不碰 LLM / 高德,只回放给定结果。"""

    def __init__(self, outcome: PlanOutcome):
        self._outcome = outcome

    async def plan_trip_with_status(self, request: Any) -> PlanOutcome:
        return self._outcome

    async def plan_trip(self, request: Any) -> TripPlan:
        return self._outcome.plan

    def describe(self) -> Dict[str, Any]:
        return {"agent_name": "FakeAgent", "framework": "test-double"}


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def install_agent(monkeypatch):
    """把 trip 路由里的 Agent 工厂替换成返回固定结果的替身。"""

    def _install(outcome: PlanOutcome) -> _FakeAgent:
        fake = _FakeAgent(outcome)
        monkeypatch.setattr(trip_routes, "get_trip_planner_agent", lambda: fake)
        return fake

    return _install


VALID_REQUEST = {
    "city": PLACEHOLDER_CITY,
    "start_date": "2025-06-01",
    "end_date": "2025-06-02",
    "travel_days": 2,
    "transportation": "公共交通",
    "accommodation": "经济型酒店",
    "preferences": ["历史文化"],
    "free_text_input": "",
}


def test_health_endpoint(client: TestClient):
    response = client.get("/health")
    assert response.status_code == 200

    body = response.json()
    assert body["status"] == "healthy"
    assert body["framework"] == "langchain+langgraph"


def test_root_endpoint(client: TestClient):
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["docs"] == "/docs"


def test_trip_health_reports_agent_runtime(client: TestClient, install_agent):
    install_agent(PlanOutcome(plan=_build_plan()))

    response = client.get("/api/trip/health")
    assert response.status_code == 200
    assert response.json()["agent_name"] == "FakeAgent"


def test_plan_success_response_shape(client: TestClient, install_agent):
    install_agent(PlanOutcome(plan=_build_plan()))

    response = client.post("/api/trip/plan", json=VALID_REQUEST)
    assert response.status_code == 200

    body = response.json()
    assert body["success"] is True
    assert body["degraded"] is False
    assert body["message"] == "旅行计划生成成功"
    assert body["data"]["city"] == PLACEHOLDER_CITY
    assert len(body["data"]["days"]) == 2
    assert body["data"]["days"][0]["attractions"][0]["location"]["longitude"] == 116.4
    assert body["used_memory"] == []
    assert body["knowledge_used"] is False
    assert body["knowledge_degraded"] is False
    assert body["knowledge_status"] == "disabled"
    assert body["knowledge_sources"] == []


def test_plan_forwards_knowledge_sources(client: TestClient, install_agent):
    install_agent(
        PlanOutcome(
            plan=_build_plan(),
            knowledge_used=True,
            knowledge_degraded=False,
            knowledge_status="ok",
            knowledge_sources=[
                KnowledgeSource(
                    id="K1",
                    chunk_id="beijing-palace-visit-001:000",
                    title="故宫博物院参观准备提示",
                    section="预约与开放",
                    publisher="项目旅行知识卡",
                    source_url="https://www.dpm.org.cn/",
                    verified_at="2026-10-20",
                    category="博物馆规则",
                )
            ],
        )
    )

    response = client.post("/api/trip/plan", json=VALID_REQUEST)
    assert response.status_code == 200

    body = response.json()
    assert body["knowledge_used"] is True
    assert body["knowledge_status"] == "ok"
    assert body["knowledge_sources"][0]["id"] == "K1"
    assert body["knowledge_sources"][0]["verified_at"] == "2026-10-20"


def test_plan_marks_placeholder_result_as_degraded(client: TestClient, install_agent):
    """回归测试:兜底占位行程不能再被当成"规划成功"。"""
    install_agent(
        PlanOutcome(
            plan=_build_plan(city=PLACEHOLDER_CITY),
            degraded=True,
            reason="三路检索均无结果",
        )
    )

    response = client.post("/api/trip/plan", json=VALID_REQUEST)
    assert response.status_code == 200

    body = response.json()
    assert body["degraded"] is True
    assert "占位" in body["message"]
    # 仍然返回数据,让前端有页面可渲染
    assert body["data"]["days"][0]["attractions"][0]["name"] == "北京景点1"


@pytest.mark.parametrize(
    "patch_body",
    [
        {"travel_days": 5},  # 与日期跨度不一致
        {"end_date": "2025-05-01"},  # 结束早于开始
        {"city": ""},  # 城市缺失
        {"travel_days": 0},  # 超出 ge=1
    ],
)
def test_plan_request_validation(client: TestClient, install_agent, patch_body):
    install_agent(PlanOutcome(plan=_build_plan()))

    payload = {**VALID_REQUEST, **patch_body}
    response = client.post("/api/trip/plan", json=payload)
    assert response.status_code == 422


def test_plan_returns_500_when_agent_crashes(client: TestClient, monkeypatch):
    class _BoomAgent:
        async def plan_trip_with_status(self, request: Any):
            raise RuntimeError("模型服务不可用")

    monkeypatch.setattr(trip_routes, "get_trip_planner_agent", lambda: _BoomAgent())

    response = client.post("/api/trip/plan", json=VALID_REQUEST)
    assert response.status_code == 500
    assert "模型服务不可用" in response.json()["detail"]
