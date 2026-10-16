"""流式规划(`/api/trip/plan/stream`)测试(离线,不联网、不花额度)

背景:一次 `/plan` 现在约 57 秒,其中规划那一次模型调用就占 25~30 秒。
时间压不到秒级,但"盯着进度条干等一分钟"是可以修的 —— 让后端每完成一步
就推一条进度事件,前端不再需要用 `setInterval` 假装进度。

这里锁住两件事:

1. ``plan_trip_stream`` 的事件序列(谁先谁后、什么时候报"生成行程开始");
2. 路由层的 SSE 分帧(`data: <json>\\n\\n`)和错误处理。
"""

import asyncio
import json
from typing import Any, Dict, List

from fastapi.testclient import TestClient

from app.agents.trip_planner_agent import MultiAgentTripPlanner
from app.api.main import app
from app.api.routes import trip as trip_routes
from app.models.schemas import Attraction, DayPlan, Location, TripPlan, TripRequest

VALID_REQUEST = {
    "city": "北京",
    "start_date": "2025-06-01",
    "end_date": "2025-06-02",
    "travel_days": 2,
    "transportation": "公共交通",
    "accommodation": "经济型酒店",
    "preferences": ["历史文化"],
    "free_text_input": "",
}


def _request() -> TripRequest:
    return TripRequest(**VALID_REQUEST)


def _plan() -> TripPlan:
    return TripPlan(
        city="北京",
        start_date="2025-06-01",
        end_date="2025-06-02",
        days=[
            DayPlan(
                date="2025-06-01",
                day_index=0,
                description="第1天",
                transportation="公共交通",
                accommodation="经济型酒店",
                attractions=[
                    Attraction(
                        name="故宫博物院",
                        address="景山前街4号",
                        location=Location(longitude=116.397, latitude=39.918),
                        visit_duration=180,
                        description="明清皇宫",
                    )
                ],
            )
        ],
        weather_info=[],
        overall_suggestions="建议提前查看开放时间。",
    )


# ---------- 图的替身 ----------


class _FakeGraph:
    """按顺序回放给定的 updates(模拟 ``stream_mode="updates"``)。"""

    def __init__(self, updates: List[Dict[str, Any]]):
        self._updates = updates

    async def astream(self, state: Any, config: Any = None, stream_mode: Any = None):
        for chunk in self._updates:
            yield chunk


class _BoomGraph:
    async def astream(self, state: Any, config: Any = None, stream_mode: Any = None):
        raise RuntimeError("模型服务不可用")
        yield {}  # 让这个函数保持为异步生成器


class _HangingGraph:
    async def astream(self, state: Any, config: Any = None, stream_mode: Any = None):
        await asyncio.sleep(3600)
        yield {}


class _StreamPlanner(MultiAgentTripPlanner):
    """绕过建图,直接喂替身图。"""

    def __init__(self, graph: Any):
        self._graph = graph

    async def _ensure_graph(self) -> Any:
        return self._graph


def _collect(graph: Any) -> List[Dict[str, Any]]:
    planner = _StreamPlanner(graph)

    async def run() -> List[Dict[str, Any]]:
        return [event async for event in planner.plan_trip_stream(_request())]

    return asyncio.run(run())


def _full_updates() -> List[Dict[str, Any]]:
    return [
        {"search_attractions": {"attraction_result": "1. 故宫博物院"}},
        {"query_weather": {"weather_result": "2025-06-01 / 晴"}},
        {"search_hotels": {"hotel_result": "1. 某酒店"}},
        {"generate_plan": {"plan": _plan()}},
    ]


# ---------- 事件序列 ----------


class TestEventSequence:
    def test_progress_order_and_result(self):
        events = _collect(_FakeGraph(_full_updates()))

        assert events[-1]["type"] == "result"
        assert events[-1]["degraded"] is False
        assert events[-1]["message"] == "旅行计划生成成功"
        assert events[-1]["data"]["city"] == "北京"

        progress = [event for event in events if event["type"] == "progress"]

        # 开头先把三路检索标成"进行中"(它们本来就是并行开始的),
        # 否则前端要等第一个节点跑完才有进度可看。
        assert [(e["label"], e["status"]) for e in progress[:3]] == [
            ("景点检索", "running"),
            ("天气查询", "running"),
            ("酒店检索", "running"),
        ]
        # 然后三路逐个 done
        assert [(e["label"], e["status"]) for e in progress[3:6]] == [
            ("景点检索", "done"),
            ("天气查询", "done"),
            ("酒店检索", "done"),
        ]
        # 最后才是"生成行程":先 running 再 done
        assert [(e["label"], e["status"]) for e in progress[6:]] == [
            ("生成行程", "running"),
            ("生成行程", "done"),
        ]
        # 每一步都带耗时,前端能直接展示
        assert all(isinstance(event["elapsed"], (int, float)) for event in progress)

    def test_generate_plan_not_marked_running_before_retrievals_finish(self):
        """三路里还有没回来的,就不能报"开始生成行程"。"""
        events = _collect(
            _FakeGraph(
                [
                    {"search_attractions": {"attraction_result": "x"}},
                    {"generate_plan": {"plan": _plan()}},
                ]
            )
        )

        generating = [
            event
            for event in events
            if event["type"] == "progress"
            and event["status"] == "running"
            and event["label"] == "生成行程"
        ]
        assert generating == []

    def test_parallel_nodes_in_one_chunk_are_all_reported(self):
        events = _collect(
            _FakeGraph(
                [
                    {
                        "search_attractions": {"attraction_result": "a"},
                        "query_weather": {"weather_result": "b"},
                        "search_hotels": {"hotel_result": "c"},
                    },
                    {"generate_plan": {"plan": _plan()}},
                ]
            )
        )

        done_labels = [
            event["label"]
            for event in events
            if event["type"] == "progress" and event["status"] == "done"
        ]
        assert done_labels == ["景点检索", "天气查询", "酒店检索", "生成行程"]


class TestDegradedPaths:
    def test_graph_exception_still_returns_a_result(self):
        """流一旦开始就不能抛 500,必须把降级结果推给前端。"""
        events = _collect(_BoomGraph())

        assert events[-1]["type"] == "result"
        assert events[-1]["degraded"] is True
        assert "规划过程出错" in events[-1]["reason"]
        # 仍然带一份可渲染的占位行程
        assert events[-1]["data"]["city"] == "北京"

    def test_empty_retrieval_is_degraded(self):
        events = _collect(
            _FakeGraph(
                [
                    {"search_attractions": {"attraction_result": ""}},
                    {"query_weather": {"weather_result": ""}},
                    {"search_hotels": {"hotel_result": ""}},
                ]
            )
        )

        assert events[-1]["degraded"] is True
        assert "三路检索均无结果" in events[-1]["reason"]

    def test_missing_plan_is_degraded(self):
        events = _collect(
            _FakeGraph(
                [
                    {"search_attractions": {"attraction_result": "a"}},
                    {"query_weather": {"weather_result": "b"}},
                    {"search_hotels": {"hotel_result": "c"}},
                ]
            )
        )

        assert events[-1]["degraded"] is True
        assert "未返回可用的结构化行程" in events[-1]["reason"]

    def test_total_timeout_degrades(self, monkeypatch):
        from app.config import get_settings

        monkeypatch.setattr(get_settings(), "plan_total_timeout", 1)
        events = _collect(_HangingGraph())

        assert events[-1]["degraded"] is True
        assert "规划超时" in events[-1]["reason"]


# ---------- 路由层 ----------


class _FakeStreamAgent:
    async def plan_trip_stream(self, request: Any):
        yield {
            "type": "progress",
            "stage": "search_attractions",
            "label": "景点检索",
            "status": "done",
            "elapsed": 8.3,
        }
        yield {
            "type": "result",
            "success": True,
            "degraded": False,
            "reason": "",
            "message": "旅行计划生成成功",
            "elapsed": 57.5,
            "data": _plan().model_dump(mode="json"),
        }


class _BoomStreamAgent:
    async def plan_trip_stream(self, request: Any):
        raise RuntimeError("模型服务不可用")
        yield {}


def _sse_payloads(body: str) -> List[Dict[str, Any]]:
    return [
        json.loads(line[len("data: ") :])
        for line in body.splitlines()
        if line.startswith("data: ")
    ]


class TestStreamRoute:
    def test_frames_events_as_sse(self, monkeypatch):
        monkeypatch.setattr(
            trip_routes, "get_trip_planner_agent", lambda: _FakeStreamAgent()
        )

        response = TestClient(app).post("/api/trip/plan/stream", json=VALID_REQUEST)

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        # 事件之间用空行分隔(SSE 的分帧要求)
        assert "\n\n" in response.text

        payloads = _sse_payloads(response.text)
        assert payloads[0]["type"] == "progress"
        assert payloads[-1]["type"] == "result"
        assert payloads[-1]["data"]["city"] == "北京"

    def test_mid_stream_failure_becomes_an_error_event(self, monkeypatch):
        monkeypatch.setattr(
            trip_routes, "get_trip_planner_agent", lambda: _BoomStreamAgent()
        )

        response = TestClient(app).post("/api/trip/plan/stream", json=VALID_REQUEST)

        # 已经开流就不能再改状态码,只能推 error 事件
        assert response.status_code == 200
        payloads = _sse_payloads(response.text)
        assert payloads[-1]["type"] == "error"
        assert "模型服务不可用" in payloads[-1]["message"]

    def test_request_validation_still_applies(self, monkeypatch):
        monkeypatch.setattr(
            trip_routes, "get_trip_planner_agent", lambda: _FakeStreamAgent()
        )

        payload = {**VALID_REQUEST, "travel_days": 5}
        response = TestClient(app).post("/api/trip/plan/stream", json=payload)

        assert response.status_code == 422
