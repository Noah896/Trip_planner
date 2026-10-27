"""高德地图服务封装(LangChain MCP)

通过 ``langchain-mcp-adapters`` 调用高德 MCP Server,并把 MCP 返回的
JSON 解析成项目内的 Pydantic 模型。

与旧版相比:
    - 不再依赖 ``hello_agents.tools.MCPTool``
    - 所有方法改为异步(与 MCP Adapter 保持一致)
    - 补全了原先标为 TODO 的结果解析
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..models.schemas import Location, POIInfo, WeatherInfo
from ..utils.city_normalization import normalize_city_name
from ..utils.json_utils import parse_mcp_payload
from .amap_mcp import extract_text, find_tool, get_amap_tools

# 高德 MCP Server 的工具名
TOOL_TEXT_SEARCH = "maps_text_search"
TOOL_WEATHER = "maps_weather"
TOOL_GEO = "maps_geo"
TOOL_DETAIL = "maps_search_detail"

TOOL_ROUTE_BY_TYPE = {
    "walking": "maps_direction_walking_by_address",
    "driving": "maps_direction_driving_by_address",
    "transit": "maps_direction_transit_integrated_by_address",
}


class AmapService:
    """高德地图服务封装类(异步)"""

    # ---------- 底层调用 ----------

    async def _call(self, tool_name: str, arguments: Dict[str, Any]) -> Optional[Any]:
        """调用高德 MCP 工具并返回解析后的数据。"""
        try:
            tools = await get_amap_tools()
            tool = find_tool(tools, tool_name)
            if tool is None:
                print(f"❌ 未找到高德工具: {tool_name}")
                return None

            result = await tool.ainvoke(arguments)
            return parse_mcp_payload(extract_text(result))
        except Exception as exc:
            print(f"❌ 调用高德工具 {tool_name} 失败: {exc}")
            return None

    # ---------- 结果解析 ----------

    @staticmethod
    def _parse_location(raw: Any) -> Optional[Location]:
        """解析经纬度,兼容字典与 "经度,纬度" 字符串两种格式。"""
        if isinstance(raw, dict):
            lng = raw.get("longitude") or raw.get("lng")
            lat = raw.get("latitude") or raw.get("lat")
            if lng is not None and lat is not None:
                try:
                    return Location(longitude=float(lng), latitude=float(lat))
                except (TypeError, ValueError):
                    return None
        if isinstance(raw, str) and "," in raw:
            lng, _, lat = raw.partition(",")
            try:
                return Location(longitude=float(lng), latitude=float(lat))
            except ValueError:
                return None
        return None

    @staticmethod
    def _to_float(value: Any, default: float = 0.0) -> float:
        """把高德返回的字符串/数字统一成 float,解析失败时回退默认值。"""
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    # 路线类型的中文名(用于生成 description)
    ROUTE_LABELS = {"walking": "步行", "driving": "驾车", "transit": "公共交通"}

    @classmethod
    def _normalize_route(
        cls,
        data: Any,
        route_type: str,
        origin_address: str,
        destination_address: str,
    ) -> Dict[str, Any]:
        """把高德路线结果归一化成 ``RouteInfo`` 可校验的结构。

        高德 direction 系列接口的典型返回是
        ``{"route": {"paths": [{"distance": "1200", "duration": "900"}]}}``
        (公交为 ``route.transits[]``)。这里做 best-effort 提取:
        解析不出距离/耗时就用 0 兜底,原始响应保留在 ``raw`` 字段里,
        从而保证 ``/api/map/route`` 在任何返回结构下都能给出合法响应,
        而不是因为 ``RouteResponse`` 校验失败变成 500。
        """
        payload = data if isinstance(data, dict) else {}
        route = payload.get("route") if isinstance(payload.get("route"), dict) else {}

        legs = (
            route.get("paths")
            or route.get("transits")
            or payload.get("paths")
            or []
        )
        first = legs[0] if legs and isinstance(legs[0], dict) else {}

        distance = cls._to_float(first.get("distance", payload.get("distance")))
        duration = int(cls._to_float(first.get("duration", payload.get("duration"))))
        label = cls.ROUTE_LABELS.get(route_type, route_type)

        if distance or duration:
            minutes = duration / 60
            duration_text = f"{minutes:.0f} 分钟" if minutes >= 1 else f"{duration} 秒"
            description = (
                f"{label}从「{origin_address}」到「{destination_address}」"
                f"全程约 {distance / 1000:.1f} 公里,预计耗时 {duration_text}"
            )
        else:
            reason = payload.get("info") or "高德未返回距离/耗时信息"
            description = (
                f"{label}从「{origin_address}」到「{destination_address}」规划完成,"
                f"但未能解析出距离与耗时({reason})"
            )

        return {
            "distance": distance,
            "duration": duration,
            "route_type": route_type,
            "description": description,
            "raw": payload,
        }

    @classmethod
    def _parse_pois(cls, data: Any) -> List[POIInfo]:
        """把高德 POI 搜索结果转换为 POIInfo 列表。

        注意:``maps_text_search`` 只返回 id / name / address / typecode,
        不含经纬度;需要坐标时请再调用 :meth:`get_poi_detail`。
        """
        if not isinstance(data, dict):
            return []

        raw_pois = data.get("pois") or data.get("data") or []
        pois: List[POIInfo] = []

        for item in raw_pois:
            if not isinstance(item, dict):
                continue
            pois.append(
                POIInfo(
                    id=str(item.get("id") or item.get("poiid") or ""),
                    name=item.get("name") or "",
                    type=item.get("type") or item.get("typecode") or "",
                    address=item.get("address") or "",
                    location=cls._parse_location(item.get("location")),
                    tel=item.get("tel"),
                )
            )

        return pois

    @classmethod
    def _parse_weather(cls, data: Any) -> List[WeatherInfo]:
        """把高德天气结果转换为 WeatherInfo 列表。

        兼容三种返回结构:
        - 实时天气 lives[]
        - 预报 forecasts[] 下直接是每日天气(本 MCP Server 的实际返回)
        - 预报 forecasts[].casts[] 嵌套(高德开放平台原始结构)
        """
        if not isinstance(data, dict):
            return []

        casts: List[Dict[str, Any]] = []

        for live in data.get("lives") or []:
            if not isinstance(live, dict):
                continue
            casts.append(cls._weather_from_day(live))

        for forecast in data.get("forecasts") or []:
            if not isinstance(forecast, dict):
                continue
            if "casts" in forecast:
                for cast in forecast.get("casts") or []:
                    if isinstance(cast, dict):
                        casts.append(cls._weather_from_day(cast))
            else:
                casts.append(cls._weather_from_day(forecast))

        weather: List[WeatherInfo] = []
        for cast in casts:
            try:
                weather.append(WeatherInfo(**cast))
            except Exception:
                continue
        return weather

    @staticmethod
    def _weather_from_day(day: Dict[str, Any]) -> Dict[str, Any]:
        """把单日天气字典映射为 WeatherInfo 的字段。"""
        return {
            "date": day.get("date", ""),
            "day_weather": day.get("dayweather") or day.get("weather", ""),
            "night_weather": day.get("nightweather") or day.get("weather", ""),
            "day_temp": day.get("daytemp") or day.get("temperature", 0),
            "night_temp": day.get("nighttemp") or day.get("temperature", 0),
            "wind_direction": day.get("daywind") or day.get("winddirection", ""),
            "wind_power": day.get("daypower") or day.get("windpower", ""),
        }

    # ---------- 对外接口 ----------

    async def search_poi(
        self, keywords: str, city: str, citylimit: bool = True
    ) -> List[POIInfo]:
        """搜索 POI

        Args:
            keywords: 搜索关键词
            city: 城市
            citylimit: 是否限制在城市范围内

        Returns:
            POI 信息列表
        """
        city = normalize_city_name(city)
        print(f"🔍 POI 搜索: keywords={keywords}, city={city}")
        data = await self._call(
            TOOL_TEXT_SEARCH,
            {
                "keywords": keywords,
                "city": city,
                "citylimit": str(citylimit).lower(),
            },
        )
        return self._parse_pois(data)

    async def get_weather(self, city: str) -> List[WeatherInfo]:
        """查询天气

        Args:
            city: 城市名称

        Returns:
            天气信息列表
        """
        city = normalize_city_name(city)
        print(f"🌤️  天气查询: city={city}")
        data = await self._call(TOOL_WEATHER, {"city": city})
        return self._parse_weather(data)

    async def plan_route(
        self,
        origin_address: str,
        destination_address: str,
        origin_city: Optional[str] = None,
        destination_city: Optional[str] = None,
        route_type: str = "walking",
    ) -> Dict[str, Any]:
        """规划路线

        Args:
            origin_address: 起点地址
            destination_address: 终点地址
            origin_city: 起点城市
            destination_city: 终点城市
            route_type: 路线类型 (walking/driving/transit)

        Returns:
            归一化后的路线信息(``RouteInfo`` 结构,原始响应在 ``raw`` 字段)
        """
        tool_name = TOOL_ROUTE_BY_TYPE.get(route_type, TOOL_ROUTE_BY_TYPE["walking"])

        arguments: Dict[str, Any] = {
            "origin_address": origin_address,
            "destination_address": destination_address,
        }
        if origin_city:
            arguments["origin_city"] = normalize_city_name(origin_city)
        if destination_city:
            arguments["destination_city"] = normalize_city_name(destination_city)

        print(f"🗺️  路线规划: {route_type} {origin_address} -> {destination_address}")
        data = await self._call(tool_name, arguments)
        return self._normalize_route(
            data, route_type, origin_address, destination_address
        )

    async def geocode(self, address: str, city: Optional[str] = None) -> Optional[Location]:
        """地理编码(地址转坐标)

        Args:
            address: 地址
            city: 城市

        Returns:
            经纬度坐标
        """
        arguments: Dict[str, Any] = {"address": address}
        if city:
            arguments["city"] = normalize_city_name(city)

        print(f"📍 地理编码: {address}")
        data = await self._call(TOOL_GEO, arguments)

        if isinstance(data, list):
            geocodes = data
        elif isinstance(data, dict):
            # 本项目实际 MCP 使用 ``return``;高德开放平台原始格式用
            # ``geocodes``,两种都兼容。
            geocodes = (
                data.get("geocodes")
                or data.get("return")
                or data.get("data")
                or []
            )
        else:
            geocodes = []

        if geocodes and isinstance(geocodes[0], dict):
            return self._parse_location(geocodes[0].get("location"))
        return None

    async def get_poi_detail(self, poi_id: str) -> Dict[str, Any]:
        """获取 POI 详情

        Args:
            poi_id: POI ID

        Returns:
            POI 详情信息
        """
        print(f"📄 获取 POI 详情: {poi_id}")
        data = await self._call(TOOL_DETAIL, {"id": poi_id})
        return data if isinstance(data, dict) else {"raw": data}


# 创建全局服务实例
_amap_service: Optional[AmapService] = None


def get_amap_service() -> AmapService:
    """获取高德地图服务实例(单例模式)"""
    global _amap_service

    if _amap_service is None:
        _amap_service = AmapService()

    return _amap_service
