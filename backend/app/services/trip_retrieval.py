"""确定性的三路检索(景点 / 天气 / 酒店),**不经过模型**。

为什么不用 Agent
----------------
原先景点 / 天气 / 酒店各自挂一个 LLM Agent,由模型自己决定调哪个工具、调几次。
实测一次 ``/plan`` 因此产生 10+ 次模型调用(单次 10~30 秒),其中检索占 8~9 次;
而且模型会"看心情"一口气发 20 个 ``maps_search_detail``(单次 ~0.9s,服务端还会
串行排队),把整条请求拖到 60~120 秒。实测数字见 AGENTS.md 第 4 节。

但检索这件事本身是确定性的 —— 关键词 → POI 列表 → 详情(坐标/评分/开放时间)。
这里直接调高德 MCP 工具并用服务层解析,换来三个好处:

1. **省掉 8~9 次模型调用**(每次 10~30 秒,且全在关键路径上);
2. **坐标由 ``maps_search_detail`` 真实回填**,不再依赖"模型记得去查"——
   ``maps_text_search`` 返回的 POI 是不带经纬度的(命名提示词要求坐标真实准确,
   模型手里却没有坐标,只能编);
3. **调用次数有固定上界**,不会再出现"模型决定调 20 次"这种失控。

模型只保留在最后一步:把三路检索结果汇总成结构化行程。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from ..models.schemas import Location, POIInfo, TripRequest, WeatherInfo
from .amap_service import AmapService, get_amap_service
from .itinerary_quality import (
    annotate_attraction_candidates,
    compact_request_reason,
    is_compact_request,
    rank_attraction_candidates,
    retain_compact_candidates,
)

# 需要回填详情(detail)的条数上限。
# detail 调用在高德 MCP 上是串行的(~0.9s/次),这个数字直接决定检索耗时:
# 太少不够规划挑,太多就是白白加时间。
ATTRACTION_MIN, ATTRACTION_MAX = 6, 12
HOTEL_MIN, HOTEL_MAX = 4, 8


def _detail_budget(travel_days: int, minimum: int, maximum: int) -> int:
    """按旅行天数估算回填多少个 POI 详情(每天 3 个的余量)。

    一天通常安排 2~3 个景点,规划需要的是"够挑",不是"越多越好"。
    """
    return min(maximum, max(minimum, travel_days * 3))


def _location_text(raw: Any) -> str:
    """把详情里的坐标归一化成 ``"lng,lat"``;拿不到就返回"未知"。"""
    if isinstance(raw, str) and "," in raw:
        return raw.strip()
    if isinstance(raw, dict):
        lng = raw.get("longitude") or raw.get("lng")
        lat = raw.get("latitude") or raw.get("lat")
        if lng is not None and lat is not None:
            return f"{lng},{lat}"
    return "未知"


def _cost_text(cost: Any) -> str:
    """把高德详情里的 ``cost`` 归一化成可读的票价文本。"""
    if isinstance(cost, list):
        parts: List[str] = []
        for item in cost:
            if isinstance(item, dict):
                price = item.get("price") or item.get("cost")
                if price:
                    parts.append(f"{item.get('ticket_type') or '门票'} {price}元")
            elif item:
                parts.append(str(item))
        return "、".join(parts)
    return str(cost).strip() if cost else ""


def _join(parts: Sequence[Any]) -> str:
    """用 `` / `` 拼接非空字段。"""
    return " / ".join(str(part) for part in parts if part not in (None, "", [], {}))


async def _collect_pois(
    service: AmapService, keywords: Sequence[str], city: str
) -> List[POIInfo]:
    """按多个关键词搜索 POI,按 id 去重并保持顺序。"""
    seen: set = set()
    collected: List[POIInfo] = []

    for keyword in keywords:
        if not keyword:
            continue
        for poi in await service.search_poi(keyword, city):
            if not poi.id or poi.id in seen:
                continue
            seen.add(poi.id)
            collected.append(poi)

    return collected


async def _resolve_city_center(
    service: AmapService,
    city: str,
) -> Optional[Location]:
    """解析城市中心。

    多一次地理编码换来可解释的距离排序。解析失败只记录日志,不影响检索主流程。
    """
    geocoder = getattr(service, "geocode", None)
    if geocoder is None:
        return None

    try:
        try:
            return await geocoder(city, city=city)
        except TypeError:
            return await geocoder(city)
    except Exception as exc:  # noqa: BLE001 - 距离增强信息不该阻断检索
        print(f"⚠️  城市中心解析失败: {type(exc).__name__}: {exc}")
        return None


async def _enrich(
    service: AmapService, pois: Sequence[POIInfo]
) -> List[Dict[str, Any]]:
    """逐个回填详情:坐标 / 评分 / 门票 / 开放时间。

    串行调用是有意的:高德 MCP Server 会把并发请求排队处理
    (实测 10 并发时单次耗时从 0.9s 涨到 7.6~9.3s,总时间与串行一样),
    并发既不省时间,还会因为频繁新建 MCP 会话而偶发连接重置。
    """
    enriched: List[Dict[str, Any]] = []

    for poi in pois:
        # 没有 id 的候选(地理编码解析出来的"用户指定")没有详情可查,直接用它的坐标
        detail = await service.get_poi_detail(poi.id) if poi.id else {}
        detail = detail if isinstance(detail, dict) else {}
        location = _location_text(detail.get("location"))
        if location == "未知" and poi.location is not None:
            location = f"{poi.location.longitude},{poi.location.latitude}"
        enriched.append(
            {
                "name": poi.name or detail.get("name") or "",
                "address": poi.address or detail.get("address") or "",
                "location": location,
                "type": detail.get("type") or poi.type or "",
                "rating": detail.get("rating") or "",
                "cost": _cost_text(detail.get("cost")),
                "opentime": detail.get("opentime2") or detail.get("opentime") or "",
                "poi_id": poi.id,
            }
        )

    return enriched


async def _resolve_must_visit(
    service: AmapService, names: Sequence[str], city: str
) -> List[POIInfo]:
    """把用户**点名的**景点解析成候选(POI 或至少一个坐标)。

    按名字搜一次;搜不到(口语简称、多字少字)就退回地理编码拿坐标 ——
    宁可坐标是地理编码来的,也不能因为"搜不到"就把用户的明确要求丢掉。
    """
    resolved: List[POIInfo] = []

    for name in names:
        if not name:
            continue

        pois = await service.search_poi(name, city)
        if pois:
            resolved.append(pois[0])
            continue

        location = await service.geocode(name, city)
        if location is not None:
            print(f"ℹ️  「{name}」搜不到 POI,用地理编码拿到了坐标")
            resolved.append(
                POIInfo(id="", name=name, type="用户指定", address="", location=location)
            )
        else:
            print(f"⚠️  「{name}」既搜不到 POI 也解析不出坐标,只能把名字带下去")

    return resolved


def _attraction_rank(poi: POIInfo) -> int:
    """风景名胜(高德 typecode ``11xxxx``)排在其它结果前面。

    用偏好词搜"美食"时也会搜出一堆餐饮 POI,直接混进景点清单会让规划把饭馆
    当景点排。这里只做**稳定排序**而不是过滤,免得把博物馆这类
    ``14xxxx``(科教文化)的候选一起丢掉。
    """
    return 0 if str(poi.type).startswith("11") else 1


def _render_attractions(items: Sequence[Dict[str, Any]]) -> str:
    lines: List[str] = []
    for index, item in enumerate(items, 1):
        distance = item.get("center_distance_km")
        distance_text = (
            f"距城市中心 {float(distance):.1f} 公里"
            if isinstance(distance, (int, float))
            else ""
        )
        lines.append(
            f"{index}. "
            + _join(
                [
                    item["name"],
                    item["address"],
                    f"经纬度 {item['location']}",
                    f"类别 {item['type']}" if item["type"] else "",
                    f"评分 {item['rating']}" if item["rating"] else "",
                    f"门票 {item['cost']}" if item["cost"] else "",
                    f"开放时间 {item['opentime']}" if item["opentime"] else "",
                    f"POI ID {item['poi_id']}" if item.get("poi_id") else "",
                    distance_text,
                ]
            )
        )
    return "\n".join(lines)


def _render_hotels(items: Sequence[Dict[str, Any]]) -> str:
    lines: List[str] = []
    for index, item in enumerate(items, 1):
        lines.append(
            f"{index}. "
            + _join(
                [
                    item["name"],
                    item["address"],
                    f"经纬度 {item['location']}",
                    f"类型 {item['type']}" if item["type"] else "",
                    f"评分 {item['rating']}" if item["rating"] else "",
                    f"价格 {item['cost']}" if item["cost"] else "",
                ]
            )
        )
    return "\n".join(lines)


def _render_weather(items: Sequence[WeatherInfo]) -> str:
    """按规划提示词期望的字段顺序渲染:日期/白天/夜间/白天温度/夜间温度/风向/风力。"""
    return "\n".join(
        " / ".join(
            [
                day.date,
                day.day_weather,
                day.night_weather,
                str(day.day_temp),
                str(day.night_temp),
                day.wind_direction,
                day.wind_power,
            ]
        )
        for day in items
    )


# ---------- 对外接口 ----------


async def retrieve_attractions(request: TripRequest) -> str:
    """检索候选景点,返回给规划 Agent 的文本清单。"""
    service = get_amap_service()
    keywords = [k for k in request.preferences if k][:2] or ["景点"]
    limit = _detail_budget(request.travel_days, ATTRACTION_MIN, ATTRACTION_MAX)
    compact = is_compact_request(request)

    try:
        # 1) 用户点名的景点**优先**:先按名字把它们解析出来,占住候选池最前面。
        #    这一步是"用户选了什么就必须有什么"的落点 —— 以前它们只以自由文本形式
        #    存在,检索根本不知道,于是规划能把用户选的地方排到角落、甚至排掉。
        pinned = await _resolve_must_visit(service, request.must_visit or [], request.city)

        # 2) 再用偏好词补充其余候选,并去掉和必去重复的
        pinned_ids = {poi.id for poi in pinned if poi.id}
        rest = await _collect_pois(service, keywords, request.city)
        rest = [poi for poi in rest if poi.id not in pinned_ids]
        rest.sort(key=_attraction_rank)

        remaining = max(0, limit - len(pinned))
        pois = pinned + rest[:remaining]
        enriched = await _enrich(service, pois)

        # 3) 详情回填后再按"景点相关性 + 城市中心距离"排序。
        #    用户点名必去项固定在原位置,不会因为位于远郊而被重排或淘汰。
        center = await _resolve_city_center(service, request.city)
        pinned_count = min(len(pinned), len(enriched))
        pinned_items = annotate_attraction_candidates(
            enriched[:pinned_count], center
        )
        ranked_rest = rank_attraction_candidates(
            enriched[pinned_count:],
            center,
            compact=compact,
        )
        if compact:
            minimum_total = min(limit, max(3, request.travel_days))
            minimum_rest = max(0, minimum_total - pinned_count)
            original_rest_count = len(ranked_rest)
            ranked_rest = retain_compact_candidates(
                ranked_rest,
                min_candidates=minimum_rest,
            )
            if len(ranked_rest) < original_rest_count:
                print(
                    "✂️  紧凑路线已过滤远端候选: "
                    f"{original_rest_count} → {len(ranked_rest)}"
                )
        items = pinned_items + ranked_rest
    except Exception as exc:  # noqa: BLE001 - 检索失败不该让整条链路 500
        print(f"⚠️  景点检索失败: {type(exc).__name__}: {exc}")
        return ""

    if pinned:
        print(f"📌 用户指定必去 {len(pinned)} 个,已置顶")
    if compact:
        reason = compact_request_reason(request) or "紧凑行程"
        print(f"🧭 已启用紧凑路线排序: {reason}")
    print(f"🔍 景点检索完成:关键词={keywords},回填 {len(items)} 个候选")
    return _render_attractions(items)


async def retrieve_weather(request: TripRequest) -> str:
    """查询天气,返回给规划 Agent 的文本。"""
    service = get_amap_service()

    try:
        weather = await service.get_weather(request.city)
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️  天气查询失败: {type(exc).__name__}: {exc}")
        return ""

    print(f"🌤️  天气查询完成:{len(weather)} 天")
    return _render_weather(weather)


async def retrieve_hotels(request: TripRequest) -> str:
    """检索酒店,返回给规划 Agent 的文本清单。"""
    service = get_amap_service()
    keywords = [request.accommodation, "酒店"] if request.accommodation else ["酒店"]
    limit = _detail_budget(request.travel_days, HOTEL_MIN, HOTEL_MAX)

    try:
        pois = await _collect_pois(service, keywords, request.city)
        items = await _enrich(service, pois[:limit])
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️  酒店检索失败: {type(exc).__name__}: {exc}")
        return ""

    print(f"🏨 酒店检索完成:回填 {len(items)} 个候选")
    return _render_hotels(items)
