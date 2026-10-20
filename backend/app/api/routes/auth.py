"""账号注册、登录和当前用户接口。"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from ...config import get_settings
from ...models.schemas import (
    AuthAccountStateResponse,
    AuthAuditEventResponse,
    AuthAuditListResponse,
    AuthChangePasswordRequest,
    AuthLoginRequest,
    AuthPasswordResetConfirmRequest,
    AuthPasswordResetResponse,
    AuthRegisterRequest,
    AuthStatusResponse,
    AuthTokenResponse,
    AuthUserResponse,
)
from ...services import auth_service
from ...services.auth_service import (
    AuthCredentialsError,
    AuthRateLimitError,
    AuthRegistrationError,
    AuthTokenError,
    AuthUserNotFoundError,
    AuthValidationError,
    IssuedToken,
)
from ..auth_dependencies import AuthContext, get_auth_context, require_admin_key

router = APIRouter(prefix="/auth", tags=["账号鉴权"])


def _client_id(request: Request) -> str:
    return request.client.host if request.client else "unknown"


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
def register(payload: AuthRegisterRequest, request: Request):
    try:
        issued = auth_service.get_auth_service().register(
            payload.username,
            payload.password,
            registration_key=payload.registration_key,
            client_id=_client_id(request),
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
def login(payload: AuthLoginRequest, request: Request):
    client_id = _client_id(request)
    try:
        issued = auth_service.get_auth_service().login(
            payload.username, payload.password, client_id=client_id
        )
    except AuthValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except AuthRateLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc
    except AuthCredentialsError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    return _token_response(issued)


@router.post(
    "/change-password",
    response_model=AuthTokenResponse,
    summary="修改当前账号密码",
    description="校验当前密码;成功后撤销该账号全部旧会话并返回新令牌。",
)
def change_password(
    payload: AuthChangePasswordRequest,
    request: Request,
    context: Optional[AuthContext] = Depends(get_auth_context),
):
    if context is None:
        raise HTTPException(status_code=404, detail="账号鉴权未启用")
    try:
        issued = auth_service.get_auth_service().change_password(
            context.user.user_id,
            payload.current_password,
            payload.new_password,
            client_id=_client_id(request),
        )
    except AuthValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (AuthCredentialsError, AuthUserNotFoundError) as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    return _token_response(issued)


@router.post(
    "/password-reset/confirm",
    response_model=AuthUserResponse,
    summary="使用一次性令牌重置密码",
    description="令牌只能使用一次;重置成功后该账号全部旧会话失效。",
)
def confirm_password_reset(
    payload: AuthPasswordResetConfirmRequest,
    request: Request,
):
    try:
        user = auth_service.get_auth_service().reset_password(
            payload.reset_token,
            payload.new_password,
            client_id=_client_id(request),
        )
    except AuthValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except AuthTokenError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return AuthUserResponse(
        user_id=user.user_id,
        username=user.username,
        created_at=user.created_at,
    )


@router.post(
    "/admin/users/{user_id}/password-reset-token",
    response_model=AuthPasswordResetResponse,
    summary="管理员签发一次性密码重置令牌",
)
def admin_issue_password_reset(
    user_id: str,
    request: Request,
    _admin: bool = Depends(require_admin_key),
):
    try:
        issued = auth_service.get_auth_service().issue_password_reset(
            user_id,
            client_id=_client_id(request),
        )
    except AuthUserNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return AuthPasswordResetResponse(
        user_id=issued.user.user_id,
        username=issued.user.username,
        reset_token=issued.reset_token,
        expires_at=issued.expires_at,
    )


def _set_account_disabled(
    user_id: str,
    *,
    disabled: bool,
    request: Request,
) -> AuthAccountStateResponse:
    try:
        account = auth_service.get_auth_service().set_user_disabled(
            user_id,
            disabled,
            client_id=_client_id(request),
        )
    except AuthUserNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return AuthAccountStateResponse(
        user_id=account.user_id,
        username=account.username,
        disabled=account.disabled,
    )


@router.post(
    "/admin/users/{user_id}/disable",
    response_model=AuthAccountStateResponse,
    summary="管理员禁用账号",
)
def admin_disable_user(
    user_id: str,
    request: Request,
    _admin: bool = Depends(require_admin_key),
):
    return _set_account_disabled(
        user_id,
        disabled=True,
        request=request,
    )


@router.post(
    "/admin/users/{user_id}/enable",
    response_model=AuthAccountStateResponse,
    summary="管理员启用账号",
)
def admin_enable_user(
    user_id: str,
    request: Request,
    _admin: bool = Depends(require_admin_key),
):
    return _set_account_disabled(
        user_id,
        disabled=False,
        request=request,
    )


@router.get(
    "/admin/audit",
    response_model=AuthAuditListResponse,
    summary="管理员查询账号生命周期审计",
)
def admin_list_audit(
    event_type: Optional[str] = Query(default=None, max_length=64),
    limit: int = Query(default=50, ge=1, le=200),
    _admin: bool = Depends(require_admin_key),
):
    events = auth_service.get_auth_service().list_audit_events(
        event_type=event_type,
        limit=limit,
    )
    return AuthAuditListResponse(
        total=len(events),
        events=[
            AuthAuditEventResponse(
                id=event.id,
                event_type=event.event_type,
                success=event.success,
                user_id=event.user_id,
                username=event.username,
                client_hash=event.client_hash,
                detail=event.detail,
                created_at=event.created_at,
            )
            for event in events
        ],
    )


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
