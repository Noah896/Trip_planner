"""用户长期记忆管理接口。"""

from typing import Literal, Optional

from fastapi import APIRouter, Path, Query

from ...models.schemas import (
    MemoryDeleteResponse,
    MemoryFactResponse,
    MemoryListResponse,
)
from ...services.memory_store import get_memory_store

router = APIRouter(prefix="/memory", tags=["记忆管理"])


@router.get(
    "/{user_id}",
    response_model=MemoryListResponse,
    summary="查询用户长期偏好",
    description=(
        "返回用户明确表达过、且当前生效的长期偏好。默认不返回已被新值取代的"
        "历史事实;排查出处时可传 `status=all`。\n\n"
        "注意:当前项目尚未接入登录鉴权,`user_id` 仍由客户端提供。"
        "生产部署前必须让 `user_id` 来自可信鉴权层,不能直接信任 URL 参数。"
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
):
    """查询一个用户的长期偏好事实。"""
    facts = get_memory_store().get_facts(
        user_id,
        status=None if status == "all" else status,
        category=category,
        key=key,
    )
    return MemoryListResponse(
        user_id=user_id.strip(),
        total=len(facts),
        facts=[MemoryFactResponse.model_validate(fact) for fact in facts],
    )


@router.delete(
    "/{user_id}",
    response_model=MemoryDeleteResponse,
    summary="删除用户全部记忆",
    description=(
        "物理删除该用户的长期偏好、历史行程和服务端会话,用于满足隐私与撤回要求。"
        "该操作不可撤销。生产部署前同样必须用可信鉴权层校验 `user_id`。"
    ),
)
def delete_memory(
    user_id: str = Path(..., min_length=1, max_length=128, description="用户 ID"),
):
    """删除一个用户保存在本服务中的全部记忆。"""
    deleted = get_memory_store().delete_user_memory(user_id)
    return MemoryDeleteResponse(
        user_id=deleted.user_id,
        deleted_total=deleted.total,
        deleted_facts=deleted.facts,
        deleted_history=deleted.history,
        deleted_sessions=deleted.sessions,
    )
