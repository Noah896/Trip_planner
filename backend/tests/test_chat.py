"""对话式入口测试(离线,不联网、不花额度)

聊天页是"两个入口并重"里的新入口,它必须满足三件事:

1. **一次模型调用就够**:意图 + 槽位 + 回复由同一次结构化输出给出,
   不能变成"先分类再回答"两次调用;
2. **结构化直传不花额外调用**:表单页直接把 `trip` 递进来时,
   必须跳过意图识别;
3. **信息不全时只追问,不硬跑**:模型没给的日期绝不替它编一个。
"""

import asyncio
import json
from datetime import date
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from app.api.main import app
from app.models.schemas import ChatMessage, ChatRequest, TripPlan, TripRequest
from app.services import chat_service
from app.services.chat_service import ChatDecision, build_trip_request


def _decision(**overrides: Any) -> ChatDecision:
    payload: Dict[str, Any] = {"intent": "plan", "reply": "好的", "city": "北京"}
    payload.update(overrides)
    return ChatDecision(**payload)


class TestBuildTripRequest:
    def test_date_range_wins_and_days_is_computed(self):
        trip = build_trip_request(
            _decision(start_date="2026-10-16", end_date="2026-10-18", travel_days=99)
        )
        assert trip is not None
        assert trip.travel_days == 3
        assert trip.start_date == date(2026, 10, 16)
        assert trip.end_date == date(2026, 10, 18)

    def test_start_date_plus_days_fills_end_date(self):
        trip = build_trip_request(_decision(start_date="2026-10-16", travel_days=3))
        assert trip is not None
        assert trip.end_date == date(2026, 10, 18)

    def test_stated_duration_is_preserved(self):
        """回归:用户说"三天"就必须是三天,不能被别的字段改掉。

        实测踩过这个坑 —— 提示词原本让模型自己推返程日期,它把"三天"算成了
        start+1,于是解析出来的条件是 2 天,生成出来的行程也就只有 2 天。
        现在提示词明确禁止模型自己推返程日,推导全部由这里的代码做。
        """
        trip = build_trip_request(_decision(start_date="2026-10-18", travel_days=3))
        assert trip is not None
        assert trip.travel_days == 3
        assert trip.end_date == date(2026, 10, 20)

    def test_end_date_plus_days_fills_start_date(self):
        trip = build_trip_request(_decision(end_date="2026-10-18", travel_days=3))
        assert trip is not None
        assert trip.start_date == date(2026, 10, 16)

    def test_reversed_dates_are_swapped(self):
        trip = build_trip_request(
            _decision(start_date="2026-10-18", end_date="2026-10-16")
        )
        assert trip is not None
        assert trip.start_date == date(2026, 10, 16)

    @pytest.mark.parametrize(
        "fmt",
        ["2026-10-16", "2026/10/16", "2026.10.16", "2026年10月16日"],
    )
    def test_common_date_formats(self, fmt: str):
        trip = build_trip_request(_decision(start_date=fmt, travel_days=2))
        assert trip is not None
        assert trip.start_date == date(2026, 10, 16)

    def test_only_days_without_any_date_is_not_enough(self):
        """只说"三天"是拼不出行程的 —— 必须追问,不能替用户挑个日期。"""
        assert build_trip_request(_decision(travel_days=3)) is None

    def test_missing_city_is_not_enough(self):
        assert build_trip_request(_decision(city=None, travel_days=3)) is None

    def test_too_many_days_is_rejected(self):
        # TripRequest 限制 30 天;超了就退回追问,而不是抛异常
        assert build_trip_request(_decision(start_date="2026-10-16", travel_days=40)) is None

    def test_defaults_and_preferences(self):
        trip = build_trip_request(
            _decision(
                start_date="2026-10-16",
                travel_days=2,
                preferences=["历史文化", "  ", "美食"],
            )
        )
        assert trip is not None
        assert trip.transportation == chat_service.DEFAULT_TRANSPORTATION
        assert trip.accommodation == chat_service.DEFAULT_ACCOMMODATION
        assert trip.preferences == ["历史文化", "美食"]


class TestDecisionTolerance:
    def test_preferences_written_as_string(self):
        """模型偶尔把数组写成 "历史文化、美食",不能因此整轮失败。"""
        decision = ChatDecision.model_validate(
            {"intent": "plan", "reply": "好", "city": "北京", "preferences": "历史文化、美食"}
        )
        assert decision.preferences == ["历史文化", "美食"]

    def test_single_preference_string(self):
        decision = ChatDecision.model_validate({"intent": "chat", "preferences": "亲子"})
        assert decision.preferences == ["亲子"]

    def test_empty_string_becomes_empty_list(self):
        decision = ChatDecision.model_validate({"intent": "chat", "preferences": ""})
        assert decision.preferences == []

    @pytest.mark.parametrize("raw", ["3", "3天", "3 天", "三天", 3.0, 3])
    def test_travel_days_is_coerced_to_int(self, raw):
        """模型实测会把天数写成 "三天" 这类字符串 —— 校验失败会让它一直重试。"""
        decision = ChatDecision.model_validate({"intent": "plan", "travel_days": raw})
        assert decision.travel_days == 3

    def test_unparseable_days_becomes_none(self):
        """读不出来就交给"信息不全→追问",不要硬失败。"""
        decision = ChatDecision.model_validate({"intent": "plan", "travel_days": "看情况"})
        assert decision.travel_days is None

    @pytest.mark.parametrize(
        "raw,expected",
        [("plan", "plan"), ("Plan", "plan"), (" plan ", "plan"), ("clarify", "clarify"), ("chat", "chat")],
    )
    def test_intent_is_normalized(self, raw, expected):
        decision = ChatDecision.model_validate({"intent": raw, "reply": "x"})
        assert decision.intent == expected


class TestScopeConstraints:
    """用户要求:没有特别说明时只要国内的景点。"""

    def test_chat_prompt_says_china_mainland_only(self):
        prompt = chat_service.CHAT_SYSTEM_PROMPT
        assert "只覆盖中国大陆" in prompt
        # 明确要求境外时不要凭记忆编
        assert "不要凭自己的记忆推荐" in prompt
        # 也不许自己挑一个城市
        assert "不要自己挑一个城市" in prompt

    def test_planner_prompt_says_use_only_retrieved_data(self):
        from app.agents.trip_planner_agent import PLANNER_AGENT_PROMPT

        assert "只能用上面提供的信息" in PLANNER_AGENT_PROMPT
        assert "只覆盖**中国大陆**" in PLANNER_AGENT_PROMPT


class TestDecideFallback:
    def test_router_failure_degrades_to_asking_instead_of_raising(self, monkeypatch):
        """实测撞过 GraphRecursionError:结构化输出失败时模型会一直重发同样参数。
        这种情况下不该把错误抛给用户,而应该退化成一次追问。"""

        class _BoomAgent:
            async def ainvoke(self, payload, config=None):
                raise RuntimeError("Recursion limit of 12 reached")

        monkeypatch.setattr(chat_service, "_get_router_agent", lambda: _BoomAgent())

        decision = asyncio.run(chat_service.decide("给我推荐几个景点", []))

        assert decision.intent == "clarify"
        assert decision.reply  # 给用户一句能看懂的话
        assert decision.missing


class TestFormatContext:
    def test_includes_today_history_and_latest(self):
        text = chat_service._format_context(
            "下周末出发",
            [
                ChatMessage(role="user", content="我想去北京"),
                ChatMessage(role="assistant", content="打算什么时候去?"),
            ],
            date(2026, 10, 16),
        )
        assert "今天是 2026-10-16" in text
        assert "用户:我想去北京" in text
        assert "助手:打算什么时候去?" in text
        assert "下周末出发" in text

    def test_empty_message_is_marked(self):
        assert "(空)" in chat_service._format_context("", [], date(2026, 10, 16))


# ---------- run_chat 的事件流 ----------


class _FakePlanner:
    def __init__(self, events: List[Dict[str, Any]]):
        self._events = events

    async def plan_trip_stream(self, trip: TripRequest):
        for event in self._events:
            yield event


def _plan_payload() -> Dict[str, Any]:
    plan = TripPlan(
        city="北京",
        start_date="2026-10-16",
        end_date="2026-10-17",
        days=[],
        weather_info=[],
        overall_suggestions="注意保暖",
    )
    return plan.model_dump(mode="json")


def _planning_events() -> List[Dict[str, Any]]:
    return [
        {"type": "progress", "stage": "search_attractions", "label": "景点检索", "status": "done", "elapsed": 8.0},
        {
            "type": "result",
            "success": True,
            "degraded": False,
            "reason": "",
            "message": "旅行计划生成成功",
            "elapsed": 40.0,
            "data": _plan_payload(),
        },
    ]


def _collect(request: ChatRequest, monkeypatch, decision=None, planner_events=None):
    if decision is not None:
        async def _fake_decide(message, history, **kwargs):
            return decision

        monkeypatch.setattr(chat_service, "decide", _fake_decide)
    monkeypatch.setattr(
        chat_service,
        "get_trip_planner_agent",
        lambda: _FakePlanner(planner_events if planner_events is not None else _planning_events()),
    )

    async def run():
        return [event async for event in chat_service.run_chat(request)]

    return asyncio.run(run())


class TestRunChat:
    def test_plan_intent_streams_progress_then_artifact(self, monkeypatch):
        events = _collect(
            ChatRequest(message="北京玩三天,下周末"),
            monkeypatch,
            decision=_decision(start_date="2026-10-16", travel_days=3, reply="好的,北京 3 天"),
        )

        types = [event["type"] for event in events]
        assert types == ["message", "progress", "artifact", "done"]

        assert events[0]["text"] == "好的,北京 3 天"

        artifact = events[2]
        assert artifact["kind"] == "trip_plan"
        assert artifact["degraded"] is False
        # 行程数据(天气/景点/酒店/预算)整份带给前端去渲染
        assert artifact["data"]["city"] == "北京"
        # 顺带把解析出来的条件带回去,前端后续"改行程"要用
        assert artifact["request"]["travel_days"] == 3

        assert events[-1]["request"]["city"] == "北京"

    def test_clarify_intent_does_not_start_planning(self, monkeypatch):
        planner = _FakePlanner(_planning_events())
        monkeypatch.setattr(chat_service, "get_trip_planner_agent", lambda: planner)

        events = _collect(
            ChatRequest(message="我想去北京"),
            monkeypatch,
            decision=_decision(intent="clarify", travel_days=None, reply="打算什么时候去?"),
        )

        assert [event["type"] for event in events] == ["message", "done"]
        assert events[0]["text"] == "打算什么时候去?"

    def test_chat_intent_just_replies(self, monkeypatch):
        events = _collect(
            ChatRequest(message="你能做什么?"),
            monkeypatch,
            decision=_decision(intent="chat", city=None, reply="我可以帮你规划行程。"),
        )
        assert [event["type"] for event in events] == ["message", "done"]

    def test_plan_intent_with_missing_slots_falls_back_to_asking(self, monkeypatch):
        """模型说能规划,但槽位拼不出来时必须追问,而不是硬跑。"""
        events = _collect(
            ChatRequest(message="北京三天"),
            monkeypatch,
            decision=_decision(travel_days=3, missing=["出发日期"]),
        )

        assert [event["type"] for event in events] == ["message", "done"]
        assert "出发日期" in events[0]["text"]

    def test_structured_passthrough_skips_intent_recognition(self, monkeypatch):
        """表单页直传 trip 时,一次额外的模型调用都不该花。"""
        async def _explode(*args, **kwargs):
            raise AssertionError("结构化直传不该调用意图识别")

        monkeypatch.setattr(chat_service, "decide", _explode)
        trip = TripRequest(
            city="北京",
            start_date="2026-10-16",
            end_date="2026-10-17",
            travel_days=2,
            transportation="公共交通",
            accommodation="经济型酒店",
        )

        events = _collect(ChatRequest(trip=trip), monkeypatch)

        types = [event["type"] for event in events]
        assert types == ["progress", "artifact", "done"]
        assert events[1]["data"]["city"] == "北京"

    def test_degraded_artifact_is_forwarded(self, monkeypatch):
        events = _collect(
            ChatRequest(message="北京三天"),
            monkeypatch,
            decision=_decision(start_date="2026-10-16", travel_days=3),
            planner_events=[
                {
                    "type": "result",
                    "success": True,
                    "degraded": True,
                    "reason": "三路检索均无结果",
                    "message": "AI 规划未完成,已返回占位行程(仅供参考,请稍后重试)",
                    "elapsed": 5.0,
                    "data": _plan_payload(),
                }
            ],
        )

        artifact = next(event for event in events if event["type"] == "artifact")
        assert artifact["degraded"] is True
        assert "占位" in artifact["message"]


class TestChatRoute:
    def _sse(self, body: str) -> List[Dict[str, Any]]:
        return [
            json.loads(line[len("data: ") :])
            for line in body.splitlines()
            if line.startswith("data: ")
        ]

    def test_streams_events(self, monkeypatch):
        events = [
            {"type": "message", "text": "好的"},
            {"type": "artifact", "kind": "trip_plan", "data": _plan_payload()},
            {"type": "done"},
        ]

        async def _fake_run_chat(payload):
            for event in events:
                yield event

        monkeypatch.setattr(chat_service, "run_chat", _fake_run_chat)

        response = TestClient(app).post("/api/chat/stream", json={"message": "北京三日游"})

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        payloads = self._sse(response.text)
        assert [p["type"] for p in payloads] == ["message", "artifact", "done"]
        assert payloads[1]["data"]["city"] == "北京"

    def test_mid_stream_failure_becomes_error_event(self, monkeypatch):
        async def _boom(payload):
            raise RuntimeError("模型服务不可用")
            yield {}

        monkeypatch.setattr(chat_service, "run_chat", _boom)

        response = TestClient(app).post("/api/chat/stream", json={"message": "北京三日游"})

        assert response.status_code == 200
        payloads = self._sse(response.text)
        assert payloads[-1]["type"] == "error"
        assert "模型服务不可用" in payloads[-1]["message"]

    def test_empty_body_is_accepted_as_continue(self, monkeypatch):
        """空 body 是合法请求(客户端可能只想带 history 接着聊)。"""
        async def _fake_run_chat(payload):
            yield {"type": "done"}

        monkeypatch.setattr(chat_service, "run_chat", _fake_run_chat)
        response = TestClient(app).post("/api/chat/stream", json={})
        assert response.status_code == 200
