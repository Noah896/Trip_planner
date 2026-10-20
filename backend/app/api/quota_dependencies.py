"""模型额度路由辅助。"""

from typing import Optional

from fastapi import HTTPException, status

from ..config import get_settings
from ..services.model_quota_service import (
    ModelQuotaExceeded,
    ModelQuotaReservation,
    get_model_quota_service,
)
from .auth_dependencies import AuthContext


def reserve_model_quota(
    context: Optional[AuthContext], units: int
) -> Optional[ModelQuotaReservation]:
    """鉴权开启时为用户原子预占额度,不足则直接返回 429。"""
    if context is None or not get_settings().model_quota_enabled:
        return None
    try:
        return get_model_quota_service().reserve(context.user.user_id, units)
    except ModelQuotaExceeded as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"今日模型调用额度已用尽(上限 {exc.limit} 单位,已用 {exc.used})"
            ),
            headers={
                "Retry-After": str(exc.retry_after),
                "X-Model-Quota-Limit": str(exc.limit),
                "X-Model-Quota-Used": str(exc.used),
                "X-Model-Quota-Remaining": "0",
                "X-Model-Quota-Reset-At": exc.reset_at,
            },
        ) from exc


def model_quota_headers(
    reservation: Optional[ModelQuotaReservation],
) -> dict[str, str]:
    """把本次预占结果暴露给客户端。"""
    if reservation is None:
        return {}
    return {
        "X-Model-Quota-Limit": str(reservation.limit),
        "X-Model-Quota-Used": str(reservation.used),
        "X-Model-Quota-Remaining": str(reservation.remaining),
        "X-Model-Quota-Reset-At": reservation.reset_at,
    }
