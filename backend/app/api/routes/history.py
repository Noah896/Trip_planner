"""当前登录用户的历史行程接口。"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status

from ...models.schemas import (
    HistoryTripDeleteResponse,
    HistoryTripDetailResponse,
    HistoryTripListResponse,
    HistoryTripSummaryResponse,
    TripPlan,
)
from ...services.memory_store import HistoryMemory, get_memory_store
from ..auth_dependencies import AuthContext, get_auth_context

router = APIRouter(prefix="/history", tags=["历史行程"])


def _require_user(context: Optional[AuthContext]) -> str:
    if context is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="历史行程需要登录后使用",
        )
    return context.user.user_id


def _summary(history: HistoryMemory) -> HistoryTripSummaryResponse:
    try:
        plan = TripPlan.model_validate(history.plan)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="历史行程数据损坏,无法读取",
        ) from exc

    requested_days = history.request.get("travel_days")
    travel_days = (
        int(requested_days)
        if isinstance(requested_days, int) and requested_days > 0
        else max(1, len(plan.days))
    )
    names = []
    seen = set()
    for day in plan.days:
        for attraction in day.attractions:
            name = attraction.name.strip()
            if name and name not in seen:
                names.append(name)
                seen.add(name)
            if len(names) >= 4:
                break
        if len(names) >= 4:
            break

    overview = " ".join(plan.overall_suggestions.split())
    return HistoryTripSummaryResponse(
        id=history.id,
        session_id=history.session_id,
        city=history.city,
        start_date=history.start_date or plan.start_date,
        end_date=history.end_date or plan.end_date,
        travel_days=travel_days,
        preferences=history.preferences,
        overview=overview[:180],
        attraction_names=names,
        created_at=history.created_at,
    )


@router.get(
    "",
    response_model=HistoryTripListResponse,
    summary="查询当前用户的历史行程",
)
def list_history(
    city: Optional[str] = Query(default=None, max_length=64),
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
    context: Optional[AuthContext] = Depends(get_auth_context),
):
    user_id = _require_user(context)
    store = get_memory_store()
    total = store.count_trip_history(user_id, city=city)
    histories = store.get_trip_history(
        user_id,
        city=city,
        limit=limit,
        offset=offset,
    )
    return HistoryTripListResponse(
        total=total,
        items=[_summary(item) for item in histories],
    )


@router.get(
    "/{history_id}",
    response_model=HistoryTripDetailResponse,
    summary="查看一条历史行程",
)
def get_history(
    history_id: int = Path(..., ge=1),
    context: Optional[AuthContext] = Depends(get_auth_context),
):
    user_id = _require_user(context)
    history = get_memory_store().get_trip_history_item(user_id, history_id)
    if history is None:
        raise HTTPException(status_code=404, detail="历史行程不存在")

    summary = _summary(history)
    try:
        plan = TripPlan.model_validate(history.plan)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="历史行程数据损坏,无法读取",
        ) from exc
    return HistoryTripDetailResponse(
        **summary.model_dump(),
        request=history.request,
        plan=plan,
    )


@router.delete(
    "/{history_id}",
    response_model=HistoryTripDeleteResponse,
    summary="删除一条历史行程",
)
def delete_history(
    history_id: int = Path(..., ge=1),
    context: Optional[AuthContext] = Depends(get_auth_context),
):
    user_id = _require_user(context)
    deleted = get_memory_store().delete_trip_history_item(user_id, history_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="历史行程不存在")
    return HistoryTripDeleteResponse(history_id=history_id)
