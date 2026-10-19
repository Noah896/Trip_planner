"""账号注册、登录和当前用户接口。"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status

from ...config import get_settings
from ...models.schemas import (
    AuthLoginRequest,
    AuthRegisterRequest,
    AuthStatusResponse,
    AuthTokenResponse,
    AuthUserResponse,
)
from ...services import auth_service
from ...services.auth_service import (
    AuthCredentialsError,
    AuthRegistrationError,
    AuthValidationError,
    IssuedToken,
)
from ..auth_dependencies import AuthContext, get_auth_context

router = APIRouter(prefix="/auth", tags=["账号鉴权"])


def _token_response(issued: IssuedToken) -> AuthTokenResponse:
    return AuthTokenResponse(
        access_token=issued.access_token,
        expires_at=issued.expires_at,
        user=AuthUserResponse(
            user_id=issued.user.user_id,
            username=issued.user.username,
            created_at=issued.user.created_at,
        ),
    )


@router.get(
    "/status",
    response_model=AuthStatusResponse,
    summary="查询鉴权与注册状态",
)
def auth_status():
    settings = get_settings()
    return AuthStatusResponse(
        enabled=settings.auth_enabled,
        registration_enabled=settings.auth_allow_registration,
        registration_requires_key=bool((settings.auth_registration_key or "").strip()),
    )


@router.post(
    "/register",
    response_model=AuthTokenResponse,
    status_code=status.HTTP_201_CREATED,
    summary="注册并登录",
)
def register(payload: AuthRegisterRequest):
    try:
        issued = auth_service.get_auth_service().register(
            payload.username,
            payload.password,
            registration_key=payload.registration_key,
        )
    except AuthValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except AuthRegistrationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return _token_response(issued)


@router.post(
    "/login",
    response_model=AuthTokenResponse,
    summary="登录",
)
def login(payload: AuthLoginRequest):
    try:
        issued = auth_service.get_auth_service().login(
            payload.username, payload.password
        )
    except AuthValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except AuthCredentialsError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    return _token_response(issued)


@router.get(
    "/me",
    response_model=AuthUserResponse,
    summary="获取当前登录用户",
)
def me(context: Optional[AuthContext] = Depends(get_auth_context)):
    if context is None:
        raise HTTPException(status_code=404, detail="账号鉴权未启用")
    return AuthUserResponse(
        user_id=context.user.user_id,
        username=context.user.username,
        created_at=context.user.created_at,
    )


@router.post(
    "/logout",
    summary="退出登录并撤销当前 token",
)
def logout(context: Optional[AuthContext] = Depends(get_auth_context)):
    if context is None:
        return {"success": True}
    auth_service.get_auth_service().revoke(context.token)
    return {"success": True}
