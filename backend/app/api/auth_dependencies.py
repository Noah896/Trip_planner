"""FastAPI Bearer 鉴权依赖。"""

import hmac
from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from ..config import get_settings
from ..services import auth_service
from ..services.auth_service import AuthTokenError, AuthUser


bearer_scheme = HTTPBearer(auto_error=False)
admin_key_scheme = APIKeyHeader(name="X-Admin-Key", auto_error=False)


@dataclass(frozen=True)
class AuthContext:
    user: AuthUser
    token: str


def get_auth_context(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> Optional[AuthContext]:
    """开启鉴权时返回当前用户,关闭时返回 None。"""
    if not get_settings().auth_enabled:
        return None
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="请先登录",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        user = auth_service.get_auth_service().authenticate(credentials.credentials)
    except AuthTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    return AuthContext(user=user, token=credentials.credentials)


def require_path_user(requested_user_id: str, context: Optional[AuthContext]) -> str:
    """记忆路径中的 user_id 必须与 Bearer token 一致。"""
    requested = (requested_user_id or "").strip()
    if context is None:
        return requested
    if requested != context.user.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="无权访问其他用户的记忆",
        )
    return context.user.user_id


def require_admin_key(
    provided_key: Optional[str] = Depends(admin_key_scheme),
) -> bool:
    """校验自托管账号管理密钥;未配置时关闭管理接口。"""
    expected = (get_settings().auth_admin_key or "").strip()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="账号管理接口未启用",
        )
    if not provided_key or not hmac.compare_digest(expected, provided_key.strip()):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="管理员密钥无效",
        )
    return True
