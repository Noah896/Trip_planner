"""历史行程列表、详情和删除接口测试。"""

from datetime import date

from fastapi.testclient import TestClient

from app.api.main import app
from app.api.routes import history as history_routes
from app.config import get_settings
from app.models.schemas import Attraction, DayPlan, Location, TripPlan, TripRequest
from app.services import auth_service
from app.services.auth_service import AuthService
from app.services.memory_store import MemoryStore


def _enable_auth(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_allow_registration", True)
    monkeypatch.setattr(settings, "auth_registration_key", "")


def _trip(user_id: str, *, city: str, session_id: str) -> TripRequest:
    return TripRequest(
        city=city,
        start_date=date(2026, 11, 1),
        end_date=date(2026, 11, 3),
        travel_days=3,
        transportation="公共交通",
        accommodation="经济型酒店",
        preferences=["历史文化"],
        user_id=user_id,
        session_id=session_id,
    )


def _plan(city: str) -> TripPlan:
    return TripPlan(
        city=city,
        start_date="2026-11-01",
        end_date="2026-11-03",
        days=[
            DayPlan(
                date="2026-11-01",
                day_index=0,
                description="历史建筑与城市漫步",
                transportation="公共交通",
                accommodation="经济型酒店",
                attractions=[
                    Attraction(
                        name=f"{city}博物馆",
                        address="测试地址",
                        location=Location(longitude=116.4, latitude=39.9),
                        visit_duration=120,
                        description="测试景点",
                    )
                ],
            )
        ],
        weather_info=[],
        overall_suggestions=f"{city}三天行程安排,注意提前预约。",
    )


def _setup(tmp_path, monkeypatch):
    _enable_auth(monkeypatch)
    auth = AuthService(tmp_path / "auth.sqlite3")
    store = MemoryStore(tmp_path / "memory.sqlite3")
    alice = auth.register("alice", "correct-password")
    bob = auth.register("bob", "correct-password")
    monkeypatch.setattr(auth_service, "get_auth_service", lambda: auth)
    monkeypatch.setattr(history_routes, "get_memory_store", lambda: store)

    store.save_successful_plan(
        session_id="alice-beijing",
        user_id=alice.user.user_id,
        request=_trip(
            alice.user.user_id,
            city="北京",
            session_id="alice-beijing",
        ),
        plan=_plan("北京"),
    )
    store.save_successful_plan(
        session_id="alice-shanghai",
        user_id=alice.user.user_id,
        request=_trip(
            alice.user.user_id,
            city="上海",
            session_id="alice-shanghai",
        ),
        plan=_plan("上海"),
    )
    store.save_successful_plan(
        session_id="bob-hangzhou",
        user_id=bob.user.user_id,
        request=_trip(
            bob.user.user_id,
            city="杭州",
            session_id="bob-hangzhou",
        ),
        plan=_plan("杭州"),
    )
    first = store.get_trip_history(alice.user.user_id, city="北京")[0]
    second = store.get_trip_history(alice.user.user_id, city="上海")[0]
    return (
        TestClient(app),
        alice,
        bob,
        first,
        second,
    )


def test_history_requires_login(tmp_path, monkeypatch):
    _enable_auth(monkeypatch)
    auth = AuthService(tmp_path / "auth.sqlite3")
    monkeypatch.setattr(auth_service, "get_auth_service", lambda: auth)

    response = TestClient(app).get("/api/history")

    assert response.status_code == 401


def test_history_list_is_isolated_and_supports_city_filter(
    tmp_path, monkeypatch
):
    client, alice, _, first, second = _setup(tmp_path, monkeypatch)
    headers = {"Authorization": f"Bearer {alice.access_token}"}

    response = client.get("/api/history", headers=headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert {item["id"] for item in payload["items"]} == {first.id, second.id}
    assert all(item["city"] in {"北京", "上海"} for item in payload["items"])
    assert all(item["attraction_names"] for item in payload["items"])

    filtered = client.get("/api/history?city=北京", headers=headers)
    assert filtered.status_code == 200
    assert filtered.json()["total"] == 1
    assert filtered.json()["items"][0]["city"] == "北京"


def test_history_detail_and_delete_enforce_ownership(tmp_path, monkeypatch):
    client, alice, bob, first, _ = _setup(tmp_path, monkeypatch)
    alice_headers = {"Authorization": f"Bearer {alice.access_token}"}
    bob_headers = {"Authorization": f"Bearer {bob.access_token}"}

    detail = client.get(f"/api/history/{first.id}", headers=alice_headers)
    assert detail.status_code == 200
    assert detail.json()["plan"]["city"] == "北京"
    assert detail.json()["request"]["travel_days"] == 3

    hidden = client.get(f"/api/history/{first.id}", headers=bob_headers)
    assert hidden.status_code == 404

    forbidden_delete = client.delete(
        f"/api/history/{first.id}",
        headers=bob_headers,
    )
    assert forbidden_delete.status_code == 404

    deleted = client.delete(f"/api/history/{first.id}", headers=alice_headers)
    assert deleted.status_code == 200
    assert deleted.json()["history_id"] == first.id
    assert client.get(f"/api/history/{first.id}", headers=alice_headers).status_code == 404
    assert client.get("/api/history", headers=alice_headers).json()["total"] == 1
