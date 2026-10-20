"""登录失败限流与按用户模型额度的离线测试。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.agents.trip_planner_agent import PlanOutcome
from app.api import quota_dependencies
from app.api.main import app
from app.api.routes import trip as trip_routes
from app.config import get_settings
from app.models.schemas import TripPlan, TripRequest
from app.services import auth_service, chat_service
from app.services.auth_service import (
    AuthCredentialsError,
    AuthRateLimitError,
    AuthService,
)
from app.services.model_quota_service import (
    ModelQuotaExceeded,
    ModelQuotaService,
)


def _enable_auth(monkeypatch, **settings_overrides: Any) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_allow_registration", True)
    monkeypatch.setattr(settings, "auth_registration_key", "")
    monkeypatch.setattr(settings, "model_quota_enabled", True)
    for name, value in settings_overrides.items():
        monkeypatch.setattr(settings, name, value)


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
        self.calls = 0

    async def plan_trip_with_status(self, request: TripRequest) -> PlanOutcome:
        self.calls += 1
        return PlanOutcome(plan=_plan())


class _StreamAgent:
    def __init__(self):
        self.calls = 0

    async def plan_trip_stream(self, request: TripRequest):
        self.calls += 1
        yield {
            "type": "result",
            "degraded": False,
            "message": "完成",
            "data": _plan().model_dump(mode="json"),
        }


def _trip_payload() -> dict[str, Any]:
    return {
        "city": "北京",
        "start_date": "2026-11-01",
        "end_date": "2026-11-03",
        "travel_days": 3,
        "transportation": "公共交通",
        "accommodation": "经济型酒店",
    }


class TestLoginRateLimit:
    def test_failures_are_limited_per_username_and_client(
        self, tmp_path, monkeypatch
    ):
        _enable_auth(
            monkeypatch,
            auth_login_max_failures=2,
            auth_login_failure_window_seconds=60,
        )
        service = AuthService(tmp_path / "auth.sqlite3")
        service.register("traveller", "correct-password")

        for _ in range(2):
            with pytest.raises(AuthCredentialsError):
                service.login(
                    "traveller",
                    "wrong-password",
                    client_id="203.0.113.10",
                )

        with pytest.raises(AuthRateLimitError) as blocked:
            service.login(
                "traveller",
                "wrong-password",
                client_id="203.0.113.10",
            )
        assert 1 <= blocked.value.retry_after <= 60

        # 另一个来源地址不受该组合的失败记录影响。
        with pytest.raises(AuthCredentialsError):
            service.login(
                "traveller",
                "wrong-password",
                client_id="203.0.113.11",
            )

    def test_successful_login_clears_failures(self, tmp_path, monkeypatch):
        _enable_auth(
            monkeypatch,
            auth_login_max_failures=2,
            auth_login_failure_window_seconds=60,
        )
        service = AuthService(tmp_path / "auth.sqlite3")
        service.register("traveller", "correct-password")
        client_id = "203.0.113.10"

        with pytest.raises(AuthCredentialsError):
            service.login("traveller", "wrong-password", client_id=client_id)
        service.login("traveller", "correct-password", client_id=client_id)

        for _ in range(2):
            with pytest.raises(AuthCredentialsError):
                service.login("traveller", "wrong-password", client_id=client_id)

    def test_failures_expire_after_sliding_window(self, tmp_path, monkeypatch):
        _enable_auth(
            monkeypatch,
            auth_login_max_failures=1,
            auth_login_failure_window_seconds=60,
        )
        clock = [datetime(2026, 10, 19, 8, 0, tzinfo=timezone.utc)]
        monkeypatch.setattr(AuthService, "_now", staticmethod(lambda: clock[0]))
        service = AuthService(tmp_path / "auth.sqlite3")
        service.register("traveller", "correct-password")
        client_id = "203.0.113.10"

        with pytest.raises(AuthCredentialsError):
            service.login("traveller", "wrong-password", client_id=client_id)
        with pytest.raises(AuthRateLimitError):
            service.login("traveller", "wrong-password", client_id=client_id)

        clock[0] += timedelta(seconds=61)
        with pytest.raises(AuthCredentialsError):
            service.login("traveller", "wrong-password", client_id=client_id)

    def test_login_api_returns_429_with_retry_after(self, tmp_path, monkeypatch):
        _enable_auth(
            monkeypatch,
            auth_login_max_failures=1,
            auth_login_failure_window_seconds=60,
        )
        service = AuthService(tmp_path / "auth.sqlite3")
        service.register("traveller", "correct-password")
        monkeypatch.setattr(auth_service, "get_auth_service", lambda: service)
        client = TestClient(app)
        payload = {"username": "traveller", "password": "wrong-password"}

        assert client.post("/api/auth/login", json=payload).status_code == 401
        response = client.post("/api/auth/login", json=payload)

        assert response.status_code == 429
        assert int(response.headers["retry-after"]) >= 1
        assert "登录失败次数过多" in response.json()["detail"]


class TestModelQuotaService:
    def test_quota_is_isolated_and_rejects_overage(self, tmp_path, monkeypatch):
        _enable_auth(monkeypatch, model_quota_daily_call_limit=3)
        service = ModelQuotaService(tmp_path / "quota.sqlite3")

        first = service.reserve("user-a", 1)
        second = service.reserve("user-a", 2)
        other = service.reserve("user-b", 1)

        assert first.remaining == 2
        assert second.remaining == 0
        assert other.remaining == 2
        with pytest.raises(ModelQuotaExceeded) as blocked:
            service.reserve("user-a", 1)
        assert blocked.value.used == 3
        assert blocked.value.retry_after >= 1

    def test_quota_resets_at_next_beijing_midnight(self, tmp_path, monkeypatch):
        _enable_auth(
            monkeypatch,
            model_quota_daily_call_limit=1,
            model_quota_utc_offset_hours=8,
        )
        clock = [datetime(2026, 10, 19, 15, 59, tzinfo=timezone.utc)]
        service = ModelQuotaService(
            tmp_path / "quota.sqlite3",
            clock=lambda: clock[0],
        )

        first = service.reserve("user-a", 1)
        with pytest.raises(ModelQuotaExceeded) as blocked:
            service.reserve("user-a", 1)
        assert 1 <= blocked.value.retry_after <= 60

        clock[0] += timedelta(minutes=2)
        next_day = service.reserve("user-a", 1)
        assert next_day.usage_day != first.usage_day
        assert next_day.remaining == 0

    def test_concurrent_reservations_never_oversell(self, tmp_path, monkeypatch):
        _enable_auth(monkeypatch, model_quota_daily_call_limit=5)
        service = ModelQuotaService(tmp_path / "quota.sqlite3")

        def attempt(_: int) -> bool:
            try:
                service.reserve("same-user", 1)
                return True
            except ModelQuotaExceeded:
                return False

        with ThreadPoolExecutor(max_workers=10) as executor:
            results = list(executor.map(attempt, range(20)))

        assert sum(results) == 5


class TestQuotaRoutes:
    def _client_and_services(self, tmp_path, monkeypatch, *, limit: int):
        _enable_auth(monkeypatch, model_quota_daily_call_limit=limit)
        auth = AuthService(tmp_path / "auth.sqlite3")
        quota = ModelQuotaService(tmp_path / "quota.sqlite3")
        issued = auth.register("traveller", "correct-password")
        monkeypatch.setattr(auth_service, "get_auth_service", lambda: auth)
        monkeypatch.setattr(
            quota_dependencies, "get_model_quota_service", lambda: quota
        )
        return (
            TestClient(app),
            {"Authorization": f"Bearer {issued.access_token}"},
        )

    def test_plan_returns_429_before_second_model_call(
        self, tmp_path, monkeypatch
    ):
        client, headers = self._client_and_services(
            tmp_path, monkeypatch, limit=1
        )
        agent = _RecordingAgent()
        monkeypatch.setattr(trip_routes, "get_trip_planner_agent", lambda: agent)
        monkeypatch.setattr(
            trip_routes,
            "_apply_memory",
            lambda request: (request, []),
        )
        monkeypatch.setattr(
            trip_routes,
            "_save_preferences",
            lambda request, *, degraded: None,
        )
        payload = _trip_payload()

        first = client.post("/api/trip/plan", headers=headers, json=payload)
        second = client.post("/api/trip/plan", headers=headers, json=payload)

        assert first.status_code == 200
        assert first.headers["x-model-quota-remaining"] == "0"
        assert second.status_code == 429
        assert int(second.headers["retry-after"]) >= 1
        assert agent.calls == 1

    def test_stream_plan_returns_429_before_second_model_call(
        self, tmp_path, monkeypatch
    ):
        client, headers = self._client_and_services(
            tmp_path, monkeypatch, limit=1
        )
        agent = _StreamAgent()
        monkeypatch.setattr(trip_routes, "get_trip_planner_agent", lambda: agent)
        monkeypatch.setattr(
            trip_routes,
            "_apply_memory",
            lambda request: (request, []),
        )
        monkeypatch.setattr(
            trip_routes,
            "_save_preferences",
            lambda request, *, degraded: None,
        )

        first = client.post(
            "/api/trip/plan/stream",
            headers=headers,
            json=_trip_payload(),
        )
        second = client.post(
            "/api/trip/plan/stream",
            headers=headers,
            json=_trip_payload(),
        )

        assert first.status_code == 200
        assert first.headers["x-model-quota-remaining"] == "0"
        assert '"type": "result"' in first.text
        assert second.status_code == 429
        assert agent.calls == 1

    def test_chat_reserves_by_entry_shape(
        self, tmp_path, monkeypatch
    ):
        client, headers = self._client_and_services(
            tmp_path, monkeypatch, limit=1
        )
        called = False

        async def fake_run_chat(payload):
            nonlocal called
            called = True
            yield {"type": "done"}

        monkeypatch.setattr(chat_service, "run_chat", fake_run_chat)
        response = client.post(
            "/api/chat/stream",
            headers=headers,
            json={"message": "你好"},
        )

        assert response.status_code == 429
        assert int(response.headers["retry-after"]) >= 1
        assert called is False

        structured = client.post(
            "/api/chat/stream",
            headers=headers,
            json={"message": "", "trip": _trip_payload()},
        )
        assert structured.status_code == 200
        assert structured.headers["x-model-quota-used"] == "1"
        assert structured.headers["x-model-quota-remaining"] == "0"
        assert called is True
