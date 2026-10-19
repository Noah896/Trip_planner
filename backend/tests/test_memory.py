"""SQLite 工作记忆与会话内 refine 的离线测试。"""

import asyncio
from datetime import date
from typing import Any, Dict, List, Optional

import pytest

from app.models.schemas import ChatRequest, TripPlan, TripRequest
from app.api.routes import trip as trip_routes
from app.services import chat_service, memory_context
from app.services.chat_service import ChatDecision
from app.services.memory_store import MemoryAccessError, MemoryStore


def _trip(**overrides: Any) -> TripRequest:
    payload: Dict[str, Any] = {
        "city": "北京",
        "start_date": date(2026, 10, 16),
        "end_date": date(2026, 10, 18),
        "travel_days": 3,
        "transportation": "公共交通",
        "accommodation": "经济型酒店",
        "preferences": ["历史文化"],
        "must_visit": ["故宫博物院"],
        "free_text_input": "带 6 岁小孩",
        "session_id": "session-1",
        "user_id": "user-1",
    }
    payload.update(overrides)
    return TripRequest(**payload)


def _plan(**overrides: Any) -> TripPlan:
    payload: Dict[str, Any] = {
        "city": "北京",
        "start_date": "2026-10-16",
        "end_date": "2026-10-18",
        "days": [],
        "weather_info": [],
        "overall_suggestions": "注意保暖",
    }
    payload.update(overrides)
    return TripPlan(**payload)


class TestMemoryStore:
    def test_save_and_load_session(self, tmp_path):
        store = MemoryStore(tmp_path / "memory.sqlite3")

        saved = store.save_successful_plan(
            session_id="session-1",
            user_id="user-1",
            request=_trip(),
            plan=_plan(),
            last_message="北京三天",
        )
        loaded = store.get_session("session-1", user_id="user-1")

        assert saved.version == 1
        assert loaded is not None
        assert loaded.request["city"] == "北京"
        assert loaded.plan["city"] == "北京"
        assert loaded.base_request["free_text_input"] == "带 6 岁小孩"

    def test_refinement_updates_current_but_preserves_base_request(self, tmp_path):
        store = MemoryStore(tmp_path / "memory.sqlite3")
        store.save_successful_plan(
            session_id="session-1",
            user_id="user-1",
            request=_trip(),
            plan=_plan(),
        )

        refined_request = _trip(free_text_input="第二天换成博物馆")
        saved = store.save_successful_plan(
            session_id="session-1",
            user_id="user-1",
            request=refined_request,
            plan=_plan(overall_suggestions="已修改第二天"),
            reset_base=False,
        )

        assert saved.version == 2
        assert saved.base_request["free_text_input"] == "带 6 岁小孩"
        assert saved.request["free_text_input"] == "第二天换成博物馆"

    def test_session_belongs_to_user(self, tmp_path):
        store = MemoryStore(tmp_path / "memory.sqlite3")
        store.save_successful_plan(
            session_id="session-1",
            user_id="user-1",
            request=_trip(),
            plan=_plan(),
        )

        with pytest.raises(MemoryAccessError):
            store.get_session("session-1", user_id="user-2")


class TestMergeRefinement:
    def test_keeps_previous_context_and_applies_edit(self):
        decision = ChatDecision(
            intent="refine",
            reply="好的",
            city=None,
            travel_days=None,
            preferences=["亲子"],
            must_visit=["中国国家博物馆"],
            free_text_input="第二天换成博物馆",
        )

        merged = chat_service.merge_refinement(
            _trip(),
            decision,
            "第二天换成博物馆",
            session_id="session-1",
            user_id="user-1",
        )

        assert merged is not None
        assert merged.city == "北京"
        assert merged.start_date == date(2026, 10, 16)
        assert merged.end_date == date(2026, 10, 18)
        assert merged.travel_days == 3
        assert merged.preferences == ["历史文化", "亲子"]
        assert merged.must_visit == ["故宫博物院", "中国国家博物馆"]
        assert merged.free_text_input == "第二天换成博物馆"

    def test_changes_duration_without_losing_start_date(self):
        decision = ChatDecision(
            intent="refine",
            reply="改成四天",
            travel_days=4,
            free_text_input="延长到四天",
        )

        merged = chat_service.merge_refinement(_trip(), decision, "改成四天")

        assert merged is not None
        assert merged.start_date == date(2026, 10, 16)
        assert merged.end_date == date(2026, 10, 19)
        assert merged.travel_days == 4

    def test_second_refinement_uses_latest_current_request(self):
        first_decision = ChatDecision(
            intent="refine",
            reply="改去上海",
            city="上海",
            free_text_input="改去上海",
        )
        first = chat_service.merge_refinement(_trip(), first_decision, "改去上海")
        assert first is not None
        assert first.city == "上海"

        second_decision = ChatDecision(
            intent="refine",
            reply="第二天换博物馆",
            free_text_input="第二天换博物馆",
        )
        second = chat_service.merge_refinement(
            first, second_decision, "第二天换博物馆"
        )

        assert second is not None
        assert second.city == "上海"
        assert second.free_text_input == "第二天换博物馆"


class _FakePlanner:
    def __init__(self, plan: TripPlan, *, degraded: bool = False):
        self.plan = plan
        self.degraded = degraded
        self.calls: List[Dict[str, Any]] = []

    async def plan_trip_stream(
        self, trip: TripRequest, previous_plan: Optional[TripPlan] = None
    ):
        self.calls.append({"trip": trip, "previous_plan": previous_plan})
        yield {
            "type": "result",
            "success": True,
            "degraded": self.degraded,
            "reason": "测试降级" if self.degraded else "",
            "message": "测试结果",
            "elapsed": 1.0,
            "data": self.plan.model_dump(mode="json"),
        }


def _run_chat(
    request: ChatRequest,
    monkeypatch,
    *,
    store: MemoryStore,
    planner: _FakePlanner,
    decision: Optional[ChatDecision] = None,
) -> List[Dict[str, Any]]:
    monkeypatch.setattr(chat_service, "get_memory_store", lambda: store)
    monkeypatch.setattr(memory_context, "get_memory_store", lambda: store)
    monkeypatch.setattr(chat_service, "get_trip_planner_agent", lambda: planner)

    if decision is not None:
        async def _fake_decide_streaming(message, history, **kwargs):
            yield {"delta": decision.reply}
            yield {"decision": decision, "streamed": decision.reply}

        monkeypatch.setattr(chat_service, "decide_streaming", _fake_decide_streaming)

    async def run():
        return [event async for event in chat_service.run_chat(request)]

    return asyncio.run(run())


class TestRunChatMemory:
    def test_chat_injects_and_saves_long_term_preferences(
        self, tmp_path, monkeypatch
    ):
        store = MemoryStore(tmp_path / "memory.sqlite3")
        store.upsert_fact(
            user_id="user-1",
            category="diet",
            key="spicy",
            value="不吃辣",
            source_text="我不吃辣",
        )
        planner = _FakePlanner(_plan())
        request = ChatRequest(
            trip=_trip(free_text_input=""),
            session_id="session-1",
            user_id="user-1",
        )

        events = _run_chat(request, monkeypatch, store=store, planner=planner)

        assert planner.calls[0]["trip"].preferences == ["历史文化", "不吃辣"]
        artifact = next(event for event in events if event["type"] == "artifact")
        assert artifact["used_memory"][0]["key"] == "spicy"
        # 回执保留用户本轮原始请求,避免把注入值误当成显式输入再次保存。
        assert artifact["request"]["preferences"] == ["历史文化"]
        assert events[-1]["used_memory"][0]["value"] == "不吃辣"
        saved = {
            (fact.category, fact.key): fact.value
            for fact in store.get_facts("user-1")
        }
        assert saved[("preference", "pref:历史文化")] == "历史文化"

    def test_successful_structured_plan_is_saved(self, tmp_path, monkeypatch):
        store = MemoryStore(tmp_path / "memory.sqlite3")
        planner = _FakePlanner(_plan())
        request = ChatRequest(trip=_trip(), session_id="session-1", user_id="user-1")

        events = _run_chat(request, monkeypatch, store=store, planner=planner)

        artifact = next(event for event in events if event["type"] == "artifact")
        assert artifact["memory_saved"] is True
        assert events[-1]["memory_version"] == 1
        assert store.get_session("session-1", user_id="user-1") is not None

    def test_refine_reuses_previous_plan_and_saves_next_version(
        self, tmp_path, monkeypatch
    ):
        store = MemoryStore(tmp_path / "memory.sqlite3")
        store.save_successful_plan(
            session_id="session-1",
            user_id="user-1",
            request=_trip(),
            plan=_plan(),
        )
        planner = _FakePlanner(_plan(overall_suggestions="第二天已换成博物馆"))
        decision = ChatDecision(
            intent="refine",
            reply="好的,把第二天换成博物馆",
            free_text_input="第二天换成博物馆",
        )

        events = _run_chat(
            ChatRequest(
                message="第二天换成博物馆",
                session_id="session-1",
                user_id="user-1",
            ),
            monkeypatch,
            store=store,
            planner=planner,
            decision=decision,
        )

        assert planner.calls[0]["previous_plan"] is not None
        assert planner.calls[0]["trip"].city == "北京"
        assert planner.calls[0]["trip"].free_text_input == "第二天换成博物馆"
        assert events[-1]["memory_version"] == 2

        loaded = store.get_session("session-1", user_id="user-1")
        assert loaded is not None
        assert loaded.base_request["free_text_input"] == "带 6 岁小孩"
        assert loaded.request["free_text_input"] == "第二天换成博物馆"

    def test_degraded_plan_is_not_saved(self, tmp_path, monkeypatch):
        store = MemoryStore(tmp_path / "memory.sqlite3")
        planner = _FakePlanner(_plan(), degraded=True)
        request = ChatRequest(trip=_trip(), session_id="session-1", user_id="user-1")

        events = _run_chat(request, monkeypatch, store=store, planner=planner)

        assert events[-1]["memory_saved"] is False
        assert store.get_session("session-1", user_id="user-1") is None
        assert store.get_facts("user-1") == []

    def test_refine_without_previous_session_asks_again(self, tmp_path, monkeypatch):
        store = MemoryStore(tmp_path / "memory.sqlite3")
        planner = _FakePlanner(_plan())
        decision = ChatDecision(
            intent="refine",
            reply="好的",
            free_text_input="第二天换成博物馆",
        )

        events = _run_chat(
            ChatRequest(message="第二天换成博物馆", session_id="missing"),
            monkeypatch,
            store=store,
            planner=planner,
            decision=decision,
        )

        assert planner.calls == []
        assert events[-1]["type"] == "done"
        assert "没有找到可修改的上一版行程" in "".join(
            event.get("text", "") for event in events
        )


class TestFormMemory:
    def test_form_helper_saves_only_successful_plan(self, tmp_path, monkeypatch):
        store = MemoryStore(tmp_path / "memory.sqlite3")
        monkeypatch.setattr(trip_routes, "get_memory_store", lambda: store)

        saved, version = trip_routes._save_memory(
            _trip(), _plan(), degraded=False
        )
        skipped, skipped_version = trip_routes._save_memory(
            _trip(session_id="session-2"), _plan(), degraded=True
        )

        assert saved is True
        assert version == 1
        assert skipped is False
        assert skipped_version is None
        assert store.get_session("session-1", user_id="user-1") is not None
        assert store.get_session("session-2", user_id="user-1") is None
