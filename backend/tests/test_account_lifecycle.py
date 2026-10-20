"""账号改密、禁用、一次性找回与审计的离线测试。"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.main import app
from app.config import get_settings
from app.services import auth_service
from app.services.auth_service import (
    AuthCredentialsError,
    AuthService,
    AuthTokenError,
)


ADMIN_KEY = "test-admin-key-" + "x" * 32


def _enable_auth(monkeypatch, **overrides: Any) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_allow_registration", True)
    monkeypatch.setattr(settings, "auth_registration_key", "")
    monkeypatch.setattr(settings, "auth_admin_key", ADMIN_KEY)
    monkeypatch.setattr(settings, "auth_password_reset_ttl_minutes", 15)
    for name, value in overrides.items():
        monkeypatch.setattr(settings, name, value)


class TestAccountLifecycleService:
    def test_change_password_revokes_every_old_session(
        self, tmp_path, monkeypatch
    ):
        _enable_auth(monkeypatch)
        service = AuthService(tmp_path / "auth.sqlite3")
        first = service.register("traveller", "correct-password")
        pending_reset = service.issue_password_reset(first.user.user_id)

        changed = service.change_password(
            first.user.user_id,
            "correct-password",
            "new-correct-password",
            client_id="203.0.113.10",
        )

        with pytest.raises(AuthTokenError):
            service.authenticate(first.access_token)
        with pytest.raises(AuthTokenError):
            service.reset_password(pending_reset.reset_token, "unexpected-password")
        assert service.authenticate(changed.access_token).user_id == first.user.user_id
        with pytest.raises(AuthCredentialsError):
            service.login("traveller", "correct-password")
        assert (
            service.login("traveller", "new-correct-password").user.user_id
            == first.user.user_id
        )

        events = service.list_audit_events(event_type="password_change")
        assert len(events) == 1
        assert events[0].success is True

    def test_disable_revokes_tokens_and_enable_restores_login(
        self, tmp_path, monkeypatch
    ):
        _enable_auth(monkeypatch)
        service = AuthService(tmp_path / "auth.sqlite3")
        issued = service.register("traveller", "correct-password")

        disabled = service.set_user_disabled(
            issued.user.user_id,
            True,
            client_id="203.0.113.10",
        )
        assert disabled.disabled is True
        with pytest.raises(AuthTokenError):
            service.authenticate(issued.access_token)
        with pytest.raises(AuthCredentialsError):
            service.login("traveller", "correct-password")

        enabled = service.set_user_disabled(
            issued.user.user_id,
            False,
            client_id="203.0.113.10",
        )
        assert enabled.disabled is False
        assert (
            service.login("traveller", "correct-password").user.user_id
            == issued.user.user_id
        )

        event_types = [
            event.event_type for event in service.list_audit_events()
        ]
        assert "account_disabled" in event_types
        assert "account_enabled" in event_types

    def test_password_reset_token_is_single_use(self, tmp_path, monkeypatch):
        _enable_auth(monkeypatch)
        service = AuthService(tmp_path / "auth.sqlite3")
        issued = service.register("traveller", "correct-password")

        reset = service.issue_password_reset(
            issued.user.user_id,
            client_id="203.0.113.10",
        )
        user = service.reset_password(
            reset.reset_token,
            "reset-password",
            client_id="203.0.113.11",
        )

        assert user.user_id == issued.user.user_id
        with pytest.raises(AuthTokenError):
            service.authenticate(issued.access_token)
        with pytest.raises(AuthTokenError):
            service.reset_password(reset.reset_token, "another-password")
        with pytest.raises(AuthCredentialsError):
            service.login("traveller", "correct-password")
        assert (
            service.login("traveller", "reset-password").user.user_id
            == issued.user.user_id
        )


class TestAccountLifecycleApi:
    def _client(self, tmp_path, monkeypatch):
        _enable_auth(monkeypatch)
        service = AuthService(tmp_path / "auth.sqlite3")
        monkeypatch.setattr(auth_service, "get_auth_service", lambda: service)
        return TestClient(app), service

    def test_change_password_api_returns_replacement_token(
        self, tmp_path, monkeypatch
    ):
        client, service = self._client(tmp_path, monkeypatch)
        issued = service.register("traveller", "correct-password")
        headers = {"Authorization": f"Bearer {issued.access_token}"}

        response = client.post(
            "/api/auth/change-password",
            headers=headers,
            json={
                "current_password": "correct-password",
                "new_password": "replacement-password",
            },
        )

        assert response.status_code == 200
        replacement = response.json()["access_token"]
        assert replacement != issued.access_token
        assert (
            client.get(
                "/api/auth/me",
                headers={"Authorization": f"Bearer {replacement}"},
            ).status_code
            == 200
        )

    def test_admin_key_protects_disable_and_reset_flow(
        self, tmp_path, monkeypatch
    ):
        client, service = self._client(tmp_path, monkeypatch)
        issued = service.register("traveller", "correct-password")
        user_id = issued.user.user_id
        admin_headers = {"X-Admin-Key": ADMIN_KEY}

        forbidden = client.post(
            f"/api/auth/admin/users/{user_id}/password-reset-token"
        )
        assert forbidden.status_code == 403

        reset_response = client.post(
            f"/api/auth/admin/users/{user_id}/password-reset-token",
            headers=admin_headers,
        )
        assert reset_response.status_code == 200
        reset_token = reset_response.json()["reset_token"]

        disabled = client.post(
            f"/api/auth/admin/users/{user_id}/disable",
            headers=admin_headers,
        )
        assert disabled.status_code == 200
        assert disabled.json()["disabled"] is True
        assert (
            client.get(
                "/api/auth/me",
                headers={"Authorization": f"Bearer {issued.access_token}"},
            ).status_code
            == 401
        )

        invalidated = client.post(
            "/api/auth/password-reset/confirm",
            json={
                "reset_token": reset_token,
                "new_password": "after-reset-password",
            },
        )
        assert invalidated.status_code == 400

        reset_response = client.post(
            f"/api/auth/admin/users/{user_id}/password-reset-token",
            headers=admin_headers,
        )
        assert reset_response.status_code == 200
        reset_token = reset_response.json()["reset_token"]

        reset = client.post(
            "/api/auth/password-reset/confirm",
            json={
                "reset_token": reset_token,
                "new_password": "after-reset-password",
            },
        )
        assert reset.status_code == 200
        assert reset.json()["user_id"] == user_id
        reused = client.post(
            "/api/auth/password-reset/confirm",
            json={
                "reset_token": reset_token,
                "new_password": "after-reset-password",
            },
        )
        assert reused.status_code == 400

        enabled = client.post(
            f"/api/auth/admin/users/{user_id}/enable",
            headers=admin_headers,
        )
        assert enabled.status_code == 200
        login = client.post(
            "/api/auth/login",
            json={
                "username": "traveller",
                "password": "after-reset-password",
            },
        )
        assert login.status_code == 200

        audit = client.get("/api/auth/admin/audit", headers=admin_headers)
        assert audit.status_code == 200
        event_types = {item["event_type"] for item in audit.json()["events"]}
        assert {
            "register",
            "password_reset_issued",
            "account_disabled",
            "password_reset_completed",
            "account_enabled",
        } <= event_types
