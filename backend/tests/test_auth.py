"""账号鉴权与接口身份绑定的离线测试。"""

from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.agents.trip_planner_agent import PlanOutcome
from app.api.main import app
from app.api.routes import trip as trip_routes
from app.config import get_settings
from app.models.schemas import TripPlan, TripRequest
from app.services import auth_service, memory_context
from app.services.auth_service import (
    AuthCredentialsError,
    AuthRegistrationError,
    AuthService,
    AuthTokenError,
    AuthValidationError,
)
from app.services.memory_store import MemoryStore


def _enable_auth(monkeypatch, *, registration_key: str = "") -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_allow_registration", True)
    monkeypatch.setattr(settings, "auth_registration_key", registration_key)
    monkeypatch.setattr(settings, "auth_session_ttl_days", 30)


class TestAuthService:
    def test_register_login_authenticate_and_revoke(self, tmp_path, monkeypatch):
        _enable_auth(monkeypatch)
        service = AuthService(tmp_path / "auth.sqlite3")

        registered = service.register("Alice_01", "correct-password")
        current = service.authenticate(registered.access_token)

        assert current.user_id == registered.user.user_id
        assert current.username == "alice_01"

        logged_in = service.login("ALICE_01", "correct-password")
        assert logged_in.user.user_id == current.user_id

        service.revoke(registered.access_token)
        with pytest.raises(AuthTokenError):
            service.authenticate(registered.access_token)
        assert service.authenticate(logged_in.access_token).user_id == current.user_id

    def test_duplicate_user_and_wrong_password(self, tmp_path, monkeypatch):
        _enable_auth(monkeypatch)
        service = AuthService(tmp_path / "auth.sqlite3")
        service.register("traveller", "correct-password")

        with pytest.raises(AuthValidationError, match="用户名已存在"):
            service.register("TRAVELLER", "another-password")
        with pytest.raises(AuthCredentialsError):
            service.login("traveller", "wrong-password")

    def test_registration_can_require_invite_key(self, tmp_path, monkeypatch):
        _enable_auth(monkeypatch, registration_key="secret-invite")
        service = AuthService(tmp_path / "auth.sqlite3")

        with pytest.raises(AuthRegistrationError):
            service.register("traveller", "correct-password")

        issued = service.register(
            "traveller",
            "correct-password",
            registration_key="secret-invite",
        )
        assert issued.user.username == "traveller"


def _plan() -> TripPlan:
    return TripPlan(
        city="北京",
        start_date="2026-11-01",
        end_date="2026-11-03",
        days=[],
        weather_info=[],
        overall_suggestions="测试行程",
    )


class _RecordingAgent:
    def __init__(self):
        self.requests: list[TripRequest] = []

    async def plan_trip_with_status(self, request: TripRequest) -> PlanOutcome:
        self.requests.append(request)
        return PlanOutcome(plan=_plan())


class TestAuthApi:
    def _client_with_service(self, tmp_path, monkeypatch):
        _enable_auth(monkeypatch)
        service = AuthService(tmp_path / "auth.sqlite3")
        monkeypatch.setattr(auth_service, "get_auth_service", lambda: service)
        return TestClient(app), service

    def test_register_and_me(self, tmp_path, monkeypatch):
        client, _ = self._client_with_service(tmp_path, monkeypatch)

        registered = client.post(
            "/api/auth/register",
            json={"username": "traveller", "password": "correct-password"},
        )

        assert registered.status_code == 201
        token = registered.json()["access_token"]
        me = client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert me.status_code == 200
        assert me.json()["username"] == "traveller"

    def test_protected_route_requires_bearer_token(self, tmp_path, monkeypatch):
        client, _ = self._client_with_service(tmp_path, monkeypatch)

        response = client.get("/api/memory/some-user")

        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"

    def test_memory_route_rejects_other_users_id(self, tmp_path, monkeypatch):
        client, service = self._client_with_service(tmp_path, monkeypatch)
        issued = service.register("traveller", "correct-password")

        response = client.get(
            "/api/memory/some-other-user",
            headers={"Authorization": f"Bearer {issued.access_token}"},
        )

        assert response.status_code == 403

    def test_trip_route_overrides_client_supplied_user_id(
        self, tmp_path, monkeypatch
    ):
        client, service = self._client_with_service(tmp_path, monkeypatch)
        issued = service.register("traveller", "correct-password")
        agent = _RecordingAgent()
        memory_store = MemoryStore(tmp_path / "memory.sqlite3")
        monkeypatch.setattr(trip_routes, "get_trip_planner_agent", lambda: agent)
        monkeypatch.setattr(
            memory_context, "get_memory_store", lambda: memory_store
        )

        response = client.post(
            "/api/trip/plan",
            headers={"Authorization": f"Bearer {issued.access_token}"},
            json={
                "city": "北京",
                "start_date": "2026-11-01",
                "end_date": "2026-11-03",
                "travel_days": 3,
                "transportation": "公共交通",
                "accommodation": "经济型酒店",
                "preferences": ["历史文化"],
                "user_id": "attacker-controlled",
            },
        )

        assert response.status_code == 200
        assert agent.requests[0].user_id == issued.user.user_id

    def test_logout_revokes_current_token(self, tmp_path, monkeypatch):
        client, service = self._client_with_service(tmp_path, monkeypatch)
        issued = service.register("traveller", "correct-password")
        headers = {"Authorization": f"Bearer {issued.access_token}"}

        assert client.post("/api/auth/logout", headers=headers).status_code == 200

        me = client.get("/api/auth/me", headers=headers)
        assert me.status_code == 401
