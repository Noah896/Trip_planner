"""显式偏好抽取、注入和 API 回执的离线测试。"""

from datetime import date
from typing import Any

from fastapi.testclient import TestClient

from app.agents.trip_planner_agent import PlanOutcome
from app.api.main import app
from app.api.routes import trip as trip_routes
from app.models.schemas import TripPlan, TripRequest
from app.services import memory_context
from app.services.memory_store import MemoryStore


def _trip(**overrides: Any) -> TripRequest:
    payload = {
        "city": "北京",
        "start_date": date(2026, 10, 16),
        "end_date": date(2026, 10, 18),
        "travel_days": 3,
        "transportation": "公共交通",
        "accommodation": "经济型酒店",
        "preferences": ["历史文化"],
        "free_text_input": "",
        "user_id": "user-1",
    }
    payload.update(overrides)
    return TripRequest(**payload)


def _plan() -> TripPlan:
    return TripPlan(
        city="北京",
        start_date="2026-10-16",
        end_date="2026-10-18",
        days=[],
        weather_info=[],
        overall_suggestions="测试行程",
    )


class _RecordingAgent:
    def __init__(self, *, degraded: bool = False):
        self.degraded = degraded
        self.request: TripRequest | None = None

    async def plan_trip_with_status(self, request: TripRequest) -> PlanOutcome:
        self.request = request
        return PlanOutcome(
            plan=_plan(),
            degraded=self.degraded,
            reason="测试降级" if self.degraded else "",
        )


class TestExplicitPreferenceExtraction:
    def test_extracts_rules_and_plain_preferences(self):
        request = _trip(
            preferences=["历史文化", "亲子"],
            free_text_input="记住我不吃辣,节奏轻松一点",
        )

        facts = memory_context.extract_explicit_preferences(request)
        mapped = {(fact.category, fact.key): fact.value for fact in facts}

        assert mapped[("diet", "spicy")] == "不吃辣"
        assert mapped[("pace", "relaxed")] == "节奏轻松"
        assert mapped[("travel", "with_children")] == "带小孩"
        assert mapped[("preference", "pref:历史文化")] == "历史文化"

    def test_one_off_refinement_is_not_saved_as_preference(self):
        request = _trip(free_text_input="把第二天换成博物馆")

        facts = memory_context.extract_explicit_preferences(request)

        assert {fact.value for fact in facts} == {"历史文化"}


class TestPreferenceInjection:
    def test_active_memory_is_fully_injected_and_reported(self, tmp_path):
        store = MemoryStore(tmp_path / "memory.sqlite3")
        stored = store.upsert_fact(
            user_id="user-1",
            category="diet",
            key="spicy",
            value="不吃辣",
            source_text="我不吃辣",
        )

        application = memory_context.apply_long_term_memory(_trip(), store=store)

        assert application.request.preferences == ["历史文化", "不吃辣"]
        assert [fact.id for fact in application.used_facts] == [stored.id]

    def test_current_explicit_value_overrides_stored_conflict(self, tmp_path):
        store = MemoryStore(tmp_path / "memory.sqlite3")
        store.upsert_fact(
            user_id="user-1",
            category="diet",
            key="spicy",
            value="不吃辣",
        )

        application = memory_context.apply_long_term_memory(
            _trip(preferences=["吃辣"]), store=store
        )

        assert application.request.preferences == ["吃辣"]
        assert application.used_facts == []

    def test_no_user_id_means_no_memory_access(self):
        application = memory_context.apply_long_term_memory(_trip(user_id=None))

        assert application.request.user_id is None
        assert application.used_facts == []

    def test_new_explicit_value_supersedes_old_memory(self, tmp_path):
        store = MemoryStore(tmp_path / "memory.sqlite3")
        store.upsert_fact(
            user_id="user-1",
            category="diet",
            key="spicy",
            value="不吃辣",
        )

        memory_context.save_explicit_preferences(
            _trip(preferences=["吃辣"]), store=store
        )

        active = {
            (fact.category, fact.key): fact.value
            for fact in store.get_facts("user-1")
        }
        assert active[("diet", "spicy")] == "吃辣"


class TestFormRouteMemory:
    def test_form_plan_injects_and_saves_explicit_preferences(
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
        agent = _RecordingAgent()
        monkeypatch.setattr(memory_context, "get_memory_store", lambda: store)
        monkeypatch.setattr(trip_routes, "get_trip_planner_agent", lambda: agent)

        response = TestClient(app).post(
            "/api/trip/plan",
            json={
                "city": "北京",
                "start_date": "2026-10-16",
                "end_date": "2026-10-18",
                "travel_days": 3,
                "transportation": "公共交通",
                "accommodation": "经济型酒店",
                "preferences": ["历史文化"],
                "user_id": "user-1",
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert agent.request is not None
        assert agent.request.preferences == ["历史文化", "不吃辣"]
        assert body["used_memory"] == [
            {
                "category": "diet",
                "key": "spicy",
                "value": "不吃辣",
                "source_text": "我不吃辣",
            }
        ]
        saved = {
            (fact.category, fact.key): fact.value
            for fact in store.get_facts("user-1", status=None)
        }
        assert saved[("preference", "pref:历史文化")] == "历史文化"

    def test_degraded_plan_does_not_write_new_preferences(
        self, tmp_path, monkeypatch
    ):
        store = MemoryStore(tmp_path / "memory.sqlite3")
        agent = _RecordingAgent(degraded=True)
        monkeypatch.setattr(memory_context, "get_memory_store", lambda: store)
        monkeypatch.setattr(trip_routes, "get_trip_planner_agent", lambda: agent)

        response = TestClient(app).post(
            "/api/trip/plan",
            json={
                "city": "北京",
                "start_date": "2026-10-16",
                "end_date": "2026-10-18",
                "travel_days": 3,
                "transportation": "公共交通",
                "accommodation": "经济型酒店",
                "preferences": ["亲子"],
                "user_id": "user-1",
            },
        )

        assert response.status_code == 200
        assert response.json()["degraded"] is True
        assert store.get_facts("user-1", status=None) == []
