"""用户长期记忆管理接口。"""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, Path, Query

from ...models.schemas import (
    MemoryDeleteResponse,
    MemoryFactResponse,
    MemoryListResponse,
)
from ...services.memory_store import get_memory_store
from ..auth_dependencies import (
    AuthContext,
    get_auth_context,
    require_path_user,
)

router = APIRouter(prefix="/memory", tags=["记忆管理"])


@router.get(
    "/{user_id}",
    response_model=MemoryListResponse,
    summary="查询用户长期偏好",
    description=(
        "返回用户明确表达过、且当前生效的长期偏好。默认不返回已被新值取代的"
        "历史事实;排查出处时可传 `status=all`。\n\n"
        "开启账号鉴权后,路径中的 `user_id` 必须与当前 Bearer token 一致。"
    ),
)
def list_memory(
    user_id: str = Path(..., min_length=1, max_length=128, description="用户 ID"),
    status: Literal["active", "superseded", "all"] = Query(
        default="active", description="事实状态"
    ),
    category: Optional[str] = Query(
        default=None, max_length=64, description="按分类过滤"
    ),
    key: Optional[str] = Query(default=None, max_length=128, description="按稳定键过滤"),
    context: Optional[AuthContext] = Depends(get_auth_context),
):
    """查询一个用户的长期偏好事实。"""
    effective_user_id = require_path_user(user_id, context)
    facts = get_memory_store().get_facts(
        effective_user_id,
        status=None if status == "all" else status,
        category=category,
        key=key,
    )
    return MemoryListResponse(
        user_id=effective_user_id,
        total=len(facts),
        facts=[MemoryFactResponse.model_validate(fact) for fact in facts],
    )


@router.delete(
    "/{user_id}",
    response_model=MemoryDeleteResponse,
    summary="删除用户全部记忆",
    description=(
        "物理删除该用户的长期偏好、历史行程和服务端会话,用于满足隐私与撤回要求。"
        "该操作不可撤销。路径中的 `user_id` 必须与当前 Bearer token 一致。"
    ),
)
def delete_memory(
    user_id: str = Path(..., min_length=1, max_length=128, description="用户 ID"),
    context: Optional[AuthContext] = Depends(get_auth_context),
):
    """删除一个用户保存在本服务中的全部记忆。"""
    effective_user_id = require_path_user(user_id, context)
    deleted = get_memory_store().delete_user_memory(effective_user_id)
    return MemoryDeleteResponse(
        user_id=deleted.user_id,
        deleted_total=deleted.total,
        deleted_facts=deleted.facts,
        deleted_history=deleted.history,
        deleted_sessions=deleted.sessions,
    )
