"""长期偏好事实与管理接口的离线测试。"""

from datetime import date

from fastapi.testclient import TestClient

from app.api.main import app
from app.api.routes import memory as memory_routes
from app.models.schemas import TripPlan, TripRequest
from app.services.memory_store import MemoryStore


def _trip() -> TripRequest:
    return TripRequest(
        city="北京",
        start_date=date(2026, 10, 16),
        end_date=date(2026, 10, 18),
        travel_days=3,
        transportation="公共交通",
        accommodation="经济型酒店",
        preferences=["历史文化"],
        session_id="session-1",
        user_id="user-1",
    )


def _plan() -> TripPlan:
    return TripPlan(
        city="北京",
        start_date="2026-10-16",
        end_date="2026-10-18",
        days=[],
        weather_info=[],
        overall_suggestions="测试行程",
    )


class TestMemoryFacts:
    def test_same_value_refreshes_source_without_duplicate(self, tmp_path):
        store = MemoryStore(tmp_path / "memory.sqlite3")

        first = store.upsert_fact(
            user_id="user-1",
            category="diet",
            key="spicy",
            value=False,
            source_text="我不吃辣",
            source_session_id="session-1",
        )
        second = store.upsert_fact(
            user_id="user-1",
            category="diet",
            key="spicy",
            value=False,
            source_text="一直都不吃辣",
            source_session_id="session-2",
        )

        assert second.id == first.id
        assert second.source_text == "一直都不吃辣"
        assert second.source_session_id == "session-2"
        assert len(store.get_facts("user-1", status=None)) == 1

    def test_changed_value_supersedes_old_fact(self, tmp_path):
        store = MemoryStore(tmp_path / "memory.sqlite3")
        old = store.upsert_fact(
            user_id="user-1",
            category="accommodation",
            key="style",
            value="经济型酒店",
            source_text="住经济型酒店",
        )

        new = store.upsert_fact(
            user_id="user-1",
            category="accommodation",
            key="style",
            value="精品民宿",
            source_text="这次想住精品民宿",
        )

        active = store.get_facts("user-1")
        all_facts = store.get_facts("user-1", status=None)
        superseded = next(fact for fact in all_facts if fact.id == old.id)

        assert [fact.id for fact in active] == [new.id]
        assert superseded.status == "superseded"
        assert superseded.superseded_by == new.id

    def test_list_filters_by_category_and_key(self, tmp_path):
        store = MemoryStore(tmp_path / "memory.sqlite3")
        store.upsert_fact(
            user_id="user-1",
            category="diet",
            key="spicy",
            value=False,
        )
        store.upsert_fact(
            user_id="user-1",
            category="travel",
            key="pace",
            value="轻松",
        )

        assert len(store.get_facts("user-1", category="diet")) == 1
        assert len(store.get_facts("user-1", key="pace")) == 1
        assert store.get_facts("user-1", category="unknown") == []

    def test_delete_user_memory_removes_all_owned_data(self, tmp_path):
        store = MemoryStore(tmp_path / "memory.sqlite3")
        store.save_successful_plan(
            session_id="session-1",
            user_id="user-1",
            request=_trip(),
            plan=_plan(),
        )
        store.upsert_fact(
            user_id="user-1",
            category="diet",
            key="spicy",
            value=False,
        )
        store.save_successful_plan(
            session_id="session-2",
            user_id="user-2",
            request=_trip().model_copy(
                update={"session_id": "session-2", "user_id": "user-2"}
            ),
            plan=_plan(),
        )
        store.upsert_fact(
            user_id="user-2",
            category="diet",
            key="spicy",
            value=True,
        )

        deleted = store.delete_user_memory("user-1")

        assert deleted.facts == 1
        assert deleted.history == 1
        assert deleted.sessions == 1
        assert deleted.total == 3
        assert store.get_facts("user-1", status=None) == []
        assert store.get_session("session-1", user_id="user-1") is None
        assert len(store.get_facts("user-2")) == 1
        assert store.get_session("session-2", user_id="user-2") is not None


class TestMemoryApi:
    def test_get_and_delete_memory(self, tmp_path, monkeypatch):
        store = MemoryStore(tmp_path / "memory.sqlite3")
        store.save_successful_plan(
            session_id="session-1",
            user_id="user-1",
            request=_trip(),
            plan=_plan(),
        )
        store.upsert_fact(
            user_id="user-1",
            category="diet",
            key="spicy",
            value=False,
            source_text="我不吃辣",
            source_session_id="session-1",
        )
        monkeypatch.setattr(memory_routes, "get_memory_store", lambda: store)
        client = TestClient(app)

        response = client.get("/api/memory/user-1")

        assert response.status_code == 200
        payload = response.json()
        assert payload["user_id"] == "user-1"
        assert payload["total"] == 1
        assert payload["facts"][0]["key"] == "spicy"
        assert payload["facts"][0]["value"] is False

        deleted = client.delete("/api/memory/user-1")

        assert deleted.status_code == 200
        assert deleted.json() == {
            "success": True,
            "user_id": "user-1",
            "deleted_total": 3,
            "deleted_facts": 1,
            "deleted_history": 1,
            "deleted_sessions": 1,
        }
        assert store.get_facts("user-1") == []
        assert store.get_session("session-1") is None

    def test_get_can_include_superseded_facts(self, tmp_path, monkeypatch):
        store = MemoryStore(tmp_path / "memory.sqlite3")
        store.upsert_fact(
            user_id="user-1",
            category="diet",
            key="spicy",
            value=True,
        )
        store.upsert_fact(
            user_id="user-1",
            category="diet",
            key="spicy",
            value=False,
        )
        monkeypatch.setattr(memory_routes, "get_memory_store", lambda: store)
        client = TestClient(app)

        active = client.get("/api/memory/user-1").json()
        all_facts = client.get("/api/memory/user-1?status=all").json()

        assert active["total"] == 1
        assert active["facts"][0]["value"] is False
        assert all_facts["total"] == 2
        assert {fact["status"] for fact in all_facts["facts"]} == {
            "active",
            "superseded",
        }
