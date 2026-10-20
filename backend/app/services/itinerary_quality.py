"""确定性的行程质量规则。

这里不调用模型,只处理已经由高德检索到的候选与已经生成的结构化行程。
目标是把"景点太远、路线来回折返"从软提示变成可排序、可评测的规则。
"""

from __future__ import annotations

from math import asin, cos, radians, sin, sqrt
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..models.schemas import Location, TripRequest


COMPACT_PREFERENCE_TERMS = (
    "亲子",
    "小孩",
    "孩子",
    "儿童",
    "宝宝",
    "老人",
    "长辈",
    "少走路",
    "少步行",
    "行动不便",
    "无障碍",
    "休闲",
    "轻松",
    "慢逛",
    "不赶",
    "不累",
)

HIGH_EFFORT_NAME_TERMS = (
    "山",
    "峡",
    "谷",
    "长城",
    "洞",
    "漂流",
    "徒步",
    "攀",
    "索道",
    "滑雪",
)

COMPACT_MAX_CENTER_DISTANCE_KM = 35.0

def parse_location(value: Any) -> Optional[Location]:
    """把 Location、字典或 ``"lng,lat"`` 统一成 Location。"""
    if isinstance(value, Location):
        return value
    if isinstance(value, Mapping):
        longitude = value.get("longitude", value.get("lng"))
        latitude = value.get("latitude", value.get("lat"))
        if longitude is not None and latitude is not None:
            try:
                return Location(
                    longitude=float(longitude),
                    latitude=float(latitude),
                )
            except (TypeError, ValueError):
                return None
    if isinstance(value, str) and "," in value:
        longitude, _, latitude = value.partition(",")
        try:
            return Location(
                longitude=float(longitude.strip()),
                latitude=float(latitude.strip()),
            )
        except ValueError:
            return None
    return None


def haversine_km(origin: Any, destination: Any) -> Optional[float]:
    """计算两个坐标的球面距离;坐标无效时返回 None。"""
    start = parse_location(origin)
    end = parse_location(destination)
    if start is None or end is None:
        return None

    longitude_delta = radians(end.longitude - start.longitude)
    latitude_delta = radians(end.latitude - start.latitude)
    start_latitude = radians(start.latitude)
    end_latitude = radians(end.latitude)

    haversine = (
        sin(latitude_delta / 2) ** 2
        + cos(start_latitude)
        * cos(end_latitude)
        * sin(longitude_delta / 2) ** 2
    )
    return 2 * 6371.0088 * asin(sqrt(haversine))


def is_compact_request(request: TripRequest) -> bool:
    """判断是否需要优先安排城市内、低折返的紧凑路线。"""
    text = " ".join(
        [
            *request.preferences,
            request.free_text_input or "",
        ]
    )
    return any(term in text for term in COMPACT_PREFERENCE_TERMS)


def compact_request_reason(request: TripRequest) -> str:
    """返回命中的精简约束词,用于提示词与日志。"""
    text = " ".join(
        [
            *request.preferences,
            request.free_text_input or "",
        ]
    )
    matched = [term for term in COMPACT_PREFERENCE_TERMS if term in text]
    return "、".join(dict.fromkeys(matched))


def _type_weight(value: Any) -> float:
    """越像景点权重越低;博物馆等文化设施仍明显优于餐饮/酒店。"""
    type_code = str(value or "")
    if type_code.startswith("11"):
        return 0.0
    if type_code.startswith("14"):
        return 0.35
    return 6.0


def _distance_band(distance_km: Optional[float]) -> int:
    if distance_km is None:
        return 2
    if distance_km <= 15:
        return 0
    if distance_km <= COMPACT_MAX_CENTER_DISTANCE_KM:
        return 1
    if distance_km <= 70:
        return 2
    return 3


def _has_high_effort_name(name: Any) -> bool:
    text = str(name or "")
    return any(term in text for term in HIGH_EFFORT_NAME_TERMS)


def annotate_attraction_candidates(
    items: Sequence[Dict[str, Any]],
    center: Any,
) -> List[Dict[str, Any]]:
    """给候选景点补充距城市中心的直线距离,不修改原字典。"""
    annotated: List[Dict[str, Any]] = []
    for item in items:
        candidate = dict(item)
        distance = haversine_km(center, candidate.get("location"))
        candidate["center_distance_km"] = (
            round(distance, 1) if distance is not None else None
        )
        annotated.append(candidate)
    return annotated


def rank_attraction_candidates(
    items: Sequence[Dict[str, Any]],
    center: Any,
    *,
    compact: bool,
) -> List[Dict[str, Any]]:
    """按景点相关性、城市中心距离和步行强度做稳定排序。

    紧凑模式把距离放到类别之前考虑,避免"远郊风景名胜"压过市内的博物馆、
    公园等更可执行的选择;普通模式仍优先保证候选确实是景点。
    """
    rows: List[Tuple[Tuple[float, float, int], Dict[str, Any]]] = []
    for index, item in enumerate(annotate_attraction_candidates(items, center)):
        distance = item.get("center_distance_km")
        numeric_distance = (
            float(distance) if isinstance(distance, (int, float)) else float("inf")
        )
        type_weight = _type_weight(item.get("type"))

        if compact:
            effort_penalty = 0.6 if _has_high_effort_name(item.get("name")) else 0.0
            score = (
                type_weight
                + _distance_band(distance) * 1.5
                + effort_penalty
            )
        else:
            score = type_weight
        rows.append(((score, numeric_distance, index), item))

    rows.sort(key=lambda row: row[0])
    return [item for _, item in rows]


def retain_compact_candidates(
    ranked_items: Sequence[Dict[str, Any]],
    *,
    min_candidates: int,
) -> List[Dict[str, Any]]:
    """紧凑模式优先保留市内候选;只有数量不足时才补入最近的远端项。"""
    if not any(
        isinstance(item.get("center_distance_km"), (int, float))
        for item in ranked_items
    ):
        return list(ranked_items)

    local: List[Dict[str, Any]] = []
    remote: List[Dict[str, Any]] = []
    for item in ranked_items:
        distance = item.get("center_distance_km")
        if (
            isinstance(distance, (int, float))
            and distance <= COMPACT_MAX_CENTER_DISTANCE_KM
        ):
            local.append(item)
        else:
            remote.append(item)

    minimum = max(0, min_candidates)
    if len(local) >= minimum:
        return local
    return local + remote[: minimum - len(local)]


def daily_route_span_km(attractions: Sequence[Any]) -> Optional[float]:
    """按给定顺序累加一天内相邻景点的直线距离。"""
    points = []
    for item in attractions:
        location = (
            item.get("location")
            if isinstance(item, Mapping)
            else getattr(item, "location", None)
        )
        parsed = parse_location(location)
        if parsed is not None:
            points.append(parsed)

    if len(points) < 2:
        return 0.0

    total = 0.0
    for origin, destination in zip(points, points[1:]):
        leg = haversine_km(origin, destination)
        if leg is None:
            return None
        total += leg
    return round(total, 1)
