"""数据模型定义"""

import contextlib
import json
from typing import Any, Dict, List, Literal, Optional, Union
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from datetime import date


# ============ 模型输出的容错解析 ============
# 背景(2026-10-16 实测):Qwen3.5 在结构化输出里会把**嵌套对象写成 JSON 字符串**,
# 例如 ``"budget": "{\"total\": 1140}"``。Pydantic 对此直接报 model_type 错误,
# 而 LangChain 的结构化输出失败后会回灌错误让模型重试 —— 模型却把**一模一样的
# 参数**再发一遍,于是无限重试到 recursion_limit。实测 6 次重试烧掉 100 秒,
# 端到端表现为 "/plan 11 分钟不返回"。
# 这类失败在源头挡掉最划算:把"长得像 JSON 的字符串"还原成对象/数组。


def _coerce_json_container(value: Any) -> Any:
    """把 JSON 字符串还原成对象/数组;其它输入原样返回。"""
    if isinstance(value, str):
        text = value.strip()
        if text[:1] in ("{", "["):
            with contextlib.suppress(ValueError):
                return json.loads(text)
    return value


def _coerce_location(value: Any) -> Any:
    """兼容坐标的三种写法:对象 / JSON 字符串 / ``"lng,lat"`` 字符串。"""
    if isinstance(value, str):
        text = value.strip()
        if text[:1] == "{":
            with contextlib.suppress(ValueError):
                return json.loads(text)
        parts = text.split(",")
        if len(parts) == 2:
            with contextlib.suppress(ValueError):
                return {"longitude": float(parts[0]), "latitude": float(parts[1])}
    return value


# ============ 请求模型 ============

class TripRequest(BaseModel):
    """旅行规划请求"""
    city: str = Field(..., min_length=1, description="目的地城市", examples=["北京"])
    start_date: date = Field(..., description="开始日期 YYYY-MM-DD", examples=["2025-06-01"])
    end_date: date = Field(..., description="结束日期 YYYY-MM-DD", examples=["2025-06-03"])
    travel_days: int = Field(..., description="旅行天数", ge=1, le=30, examples=[3])
    transportation: str = Field(..., description="交通方式", examples=["公共交通"])
    accommodation: str = Field(..., description="住宿偏好", examples=["经济型酒店"])
    preferences: List[str] = Field(
        default=[], description="旅行偏好标签", examples=[["历史文化", "美食"]]
    )
    must_visit: List[str] = Field(
        default=[],
        description=(
            "用户**明确点名**要去的景点/地点。与 preferences 不同,这是硬约束:"
            "检索会先按名字把它们找出来放进候选池,规划也必须安排进行程"
        ),
        examples=[["故宫博物院"]],
    )
    free_text_input: Optional[str] = Field(
        default="", description="额外要求", examples=["希望多安排一些博物馆"]
    )
    session_id: Optional[str] = Field(
        default=None,
        description="服务端会话 ID。提供后会把成功行程保存到 SQLite,供后续修改",
        max_length=128,
    )
    user_id: Optional[str] = Field(
        default=None,
        description="稳定用户 ID。多用户部署时应由鉴权层写入,不能直接信任客户端",
        max_length=128,
    )

    @model_validator(mode="after")
    def _validate_date_range(self) -> "TripRequest":
        """校验日期区间合法,并与 travel_days 保持一致。

        日期字段由 ``str`` 改为 ``date`` 后,格式错误会在请求校验阶段直接
        返回 422,而不是让脏数据流到兜底逻辑里由 ``strptime`` 抛异常、
        最终变成 500。
        """
        if self.end_date < self.start_date:
            raise ValueError("结束日期不能早于开始日期")

        expected_days = (self.end_date - self.start_date).days + 1
        if self.travel_days != expected_days:
            raise ValueError(
                f"travel_days({self.travel_days}天)与日期跨度"
                f"({self.start_date} ~ {self.end_date},共 {expected_days} 天)不一致"
            )

        return self

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "city": "北京",
                "start_date": "2025-06-01",
                "end_date": "2025-06-03",
                "travel_days": 3,
                "transportation": "公共交通",
                "accommodation": "经济型酒店",
                "preferences": ["历史文化", "美食"],
                "free_text_input": "希望多安排一些博物馆",
            }
        }
    )


class POISearchRequest(BaseModel):
    """POI搜索请求"""
    keywords: str = Field(..., description="搜索关键词", examples=["故宫"])
    city: str = Field(..., description="城市", examples=["北京"])
    citylimit: bool = Field(default=True, description="是否限制在城市范围内")


class RouteRequest(BaseModel):
    """路线规划请求"""
    origin_address: str = Field(..., description="起点地址", examples=["北京市朝阳区阜通东大街6号"])
    destination_address: str = Field(..., description="终点地址", examples=["北京市海淀区上地十街10号"])
    origin_city: Optional[str] = Field(default=None, description="起点城市")
    destination_city: Optional[str] = Field(default=None, description="终点城市")
    route_type: str = Field(default="walking", description="路线类型: walking/driving/transit")


# ============ 响应模型 ============

class Location(BaseModel):
    """地理位置"""
    longitude: float = Field(..., description="经度")
    latitude: float = Field(..., description="纬度")

    @model_validator(mode="before")
    @classmethod
    def _accept_string_forms(cls, data: Any) -> Any:
        return _coerce_location(data)


class Attraction(BaseModel):
    """景点信息"""
    name: str = Field(..., description="景点名称")
    address: str = Field(..., description="地址")
    location: Location = Field(..., description="经纬度坐标")
    visit_duration: int = Field(..., description="建议游览时间(分钟)")
    description: str = Field(..., description="景点描述")
    category: Optional[str] = Field(default="景点", description="景点类别")
    rating: Optional[float] = Field(default=None, description="评分")
    photos: Optional[List[str]] = Field(default_factory=list, description="景点图片URL列表")
    poi_id: Optional[str] = Field(default="", description="POI ID")
    image_url: Optional[str] = Field(default=None, description="图片URL")
    ticket_price: int = Field(default=0, description="门票价格(元)")

    @field_validator("photos", mode="before")
    @classmethod
    def _accept_json_photos(cls, v: Any) -> Any:
        return _coerce_json_container(v)


class Meal(BaseModel):
    """餐饮信息"""
    type: str = Field(..., description="餐饮类型: breakfast/lunch/dinner/snack")
    name: str = Field(..., description="餐饮名称")
    address: Optional[str] = Field(default=None, description="地址")
    location: Optional[Location] = Field(default=None, description="经纬度坐标")
    description: Optional[str] = Field(default=None, description="描述")
    estimated_cost: int = Field(default=0, description="预估费用(元)")

    @field_validator("location", mode="before")
    @classmethod
    def _accept_string_location(cls, v: Any) -> Any:
        return _coerce_location(v)


class Hotel(BaseModel):
    """酒店信息"""
    name: str = Field(..., description="酒店名称")
    address: str = Field(default="", description="酒店地址")
    location: Optional[Location] = Field(default=None, description="酒店位置")
    price_range: str = Field(default="", description="价格范围")
    rating: str = Field(default="", description="评分")
    distance: str = Field(default="", description="距离景点距离")
    type: str = Field(default="", description="酒店类型")
    estimated_cost: int = Field(default=0, description="预估费用(元/晚)")

    @field_validator("location", mode="before")
    @classmethod
    def _accept_string_location(cls, v: Any) -> Any:
        return _coerce_location(v)


class DayPlan(BaseModel):
    """单日行程"""
    date: str = Field(..., description="日期 YYYY-MM-DD")
    day_index: int = Field(..., description="第几天(从0开始)")
    description: str = Field(..., description="当日行程描述")
    transportation: str = Field(..., description="交通方式")
    accommodation: str = Field(..., description="住宿")
    hotel: Optional[Hotel] = Field(default=None, description="推荐酒店")
    attractions: List[Attraction] = Field(default=[], description="景点列表")
    meals: List[Meal] = Field(default=[], description="餐饮列表")

    @field_validator("hotel", "attractions", "meals", mode="before")
    @classmethod
    def _accept_json_containers(cls, v: Any) -> Any:
        return _coerce_json_container(v)


class WeatherInfo(BaseModel):
    """天气信息"""
    date: str = Field(..., description="日期 YYYY-MM-DD")
    day_weather: str = Field(default="", description="白天天气")
    night_weather: str = Field(default="", description="夜间天气")
    day_temp: Union[int, str] = Field(default=0, description="白天温度")
    night_temp: Union[int, str] = Field(default=0, description="夜间温度")
    wind_direction: str = Field(default="", description="风向")
    wind_power: str = Field(default="", description="风力")

    @field_validator('day_temp', 'night_temp', mode='before')
    @classmethod
    def parse_temperature(cls, v):
        """解析温度,移除°C等单位"""
        if isinstance(v, str):
            # 移除°C, ℃等单位符号
            v = v.replace('°C', '').replace('℃', '').replace('°', '').strip()
            try:
                return int(v)
            except ValueError:
                return 0
        return v


class Budget(BaseModel):
    """预算信息"""
    total_attractions: int = Field(default=0, description="景点门票总费用")
    total_hotels: int = Field(default=0, description="酒店总费用")
    total_meals: int = Field(default=0, description="餐饮总费用")
    total_transportation: int = Field(default=0, description="交通总费用")
    total: int = Field(default=0, description="总费用")


class TripPlan(BaseModel):
    """旅行计划"""
    city: str = Field(..., description="目的地城市")
    start_date: str = Field(..., description="开始日期")
    end_date: str = Field(..., description="结束日期")
    days: List[DayPlan] = Field(..., description="每日行程")
    weather_info: List[WeatherInfo] = Field(default=[], description="天气信息")
    overall_suggestions: str = Field(..., description="总体建议")
    budget: Optional[Budget] = Field(default=None, description="预算信息")

    @field_validator("days", "weather_info", "budget", mode="before")
    @classmethod
    def _accept_json_containers(cls, v: Any) -> Any:
        """模型把 days/weather_info/budget 写成 JSON 字符串时的兜底(实测必现)。"""
        return _coerce_json_container(v)


class UsedMemoryResponse(BaseModel):
    """本轮规划实际注入的一条长期偏好。"""

    category: str = Field(..., description="偏好分类")
    key: str = Field(..., description="稳定键")
    value: str = Field(..., description="注入的偏好值")
    source_text: str = Field(default="", description="原始出处文本")


class HistoryUsageResponse(BaseModel):
    """本轮规划引用的历史行程摘要。"""

    history_id: int = Field(..., description="历史行程 ID")
    city: str = Field(..., description="历史目的地城市")
    start_date: str = Field(..., description="历史出发日期")
    end_date: str = Field(..., description="历史返程日期")
    travel_days: int = Field(..., description="历史旅行天数")
    preferences: List[str] = Field(default=[], description="历史偏好快照")


class TripPlanResponse(BaseModel):
    """旅行计划响应"""
    success: bool = Field(..., description="是否成功")
    degraded: bool = Field(
        default=False,
        description=(
            "是否为降级结果。True 表示 AI 规划未成功,返回的是占位行程"
            "(景点/餐饮为占位内容),前端应提示用户不要直接采用"
        ),
    )
    message: str = Field(default="", description="消息")
    data: Optional[TripPlan] = Field(default=None, description="旅行计划数据")
    session_id: Optional[str] = Field(default=None, description="服务端会话 ID")
    memory_saved: bool = Field(default=False, description="本次成功行程是否写入会话记忆")
    memory_version: Optional[int] = Field(default=None, description="写入后的会话版本")
    used_memory: List[UsedMemoryResponse] = Field(
        default=[], description="本轮规划实际使用的长期偏好"
    )
    used_history: Optional[HistoryUsageResponse] = Field(
        default=None, description="本轮规划引用的历史行程"
    )


class MemoryFactResponse(BaseModel):
    """一条长期偏好事实的对外表示。"""

    id: int = Field(..., description="事实 ID")
    user_id: str = Field(..., description="所属用户 ID")
    category: str = Field(..., description="偏好分类")
    key: str = Field(..., description="同一分类下的稳定键")
    value: Any = Field(..., description="用户明确表达的偏好值")
    source_text: str = Field(default="", description="原始出处文本")
    source_session_id: Optional[str] = Field(default=None, description="来源会话 ID")
    confidence: float = Field(..., ge=0, le=1, description="可信度")
    status: str = Field(..., description="active / superseded")
    created_at: str = Field(..., description="创建时间")
    updated_at: str = Field(..., description="更新时间")
    superseded_by: Optional[int] = Field(
        default=None, description="被哪条新事实取代"
    )

    model_config = ConfigDict(from_attributes=True)


class MemoryListResponse(BaseModel):
    """用户当前生效的长期偏好。"""

    user_id: str = Field(..., description="用户 ID")
    total: int = Field(..., ge=0, description="返回条数")
    facts: List[MemoryFactResponse] = Field(default=[], description="偏好事实")


class MemoryDeleteResponse(BaseModel):
    """删除用户全部记忆后的计数。"""

    success: bool = Field(default=True, description="是否完成删除")
    user_id: str = Field(..., description="用户 ID")
    deleted_total: int = Field(..., ge=0, description="删除记录总数")
    deleted_facts: int = Field(..., ge=0, description="删除的偏好事实数")
    deleted_history: int = Field(..., ge=0, description="删除的历史行程数")
    deleted_sessions: int = Field(..., ge=0, description="删除的会话数")


class POIInfo(BaseModel):
    """POI信息"""
    id: str = Field(..., description="POI ID")
    name: str = Field(..., description="名称")
    type: str = Field(..., description="类型")
    address: str = Field(..., description="地址")
    location: Optional[Location] = Field(
        default=None, description="经纬度坐标(高德文本搜索结果可能不含,需再查详情)"
    )
    tel: Optional[str] = Field(default=None, description="电话")


class POISearchResponse(BaseModel):
    """POI搜索响应"""
    success: bool = Field(..., description="是否成功")
    message: str = Field(default="", description="消息")
    data: List[POIInfo] = Field(default=[], description="POI列表")


class RouteInfo(BaseModel):
    """路线信息"""
    distance: float = Field(..., description="距离(米)")
    duration: int = Field(..., description="时间(秒)")
    route_type: str = Field(..., description="路线类型")
    description: str = Field(..., description="路线描述")
    raw: Optional[Dict[str, Any]] = Field(
        default=None, description="高德原始返回结构(便于前端扩展与排查问题)"
    )


class RouteResponse(BaseModel):
    """路线规划响应"""
    success: bool = Field(..., description="是否成功")
    message: str = Field(default="", description="消息")
    data: Optional[RouteInfo] = Field(default=None, description="路线信息")


class WeatherResponse(BaseModel):
    """天气查询响应"""
    success: bool = Field(..., description="是否成功")
    message: str = Field(default="", description="消息")
    data: List[WeatherInfo] = Field(default=[], description="天气信息")


# ============ 错误响应 ============

class ErrorResponse(BaseModel):
    """错误响应"""
    success: bool = Field(default=False, description="是否成功")
    message: str = Field(..., description="错误消息")
    error_code: Optional[str] = Field(default=None, description="错误代码")


# ============ 聊天式入口 ============


class ChatMessage(BaseModel):
    """一条对话消息(只用于把上下文喂给意图识别)。"""

    role: Literal["user", "assistant"] = Field(..., description="谁说的")
    content: str = Field(default="", description="内容")


class ChatRequest(BaseModel):
    """聊天式规划的请求体。

    两种用法:

    1. **自然语言**(聊天页):只给 ``message``(可带 ``history``),由后端判断意图、
       抽取槽位,信息够了就直接生成行程;
    2. **结构化直传**(表单页):直接给 ``trip``,此时**跳过意图识别**,
       一次模型调用都不额外花 —— 这就是"两个入口共用一套流水线"的接缝。
    """

    message: str = Field(default="", description="用户这句话", examples=["北京玩三天,带小孩"])
    history: List[ChatMessage] = Field(
        default_factory=list, description="之前的对话(用于补全条件,建议不超过 10 条)"
    )
    trip: Optional[TripRequest] = Field(
        default=None,
        description="已经填好的结构化请求。给了它就跳过意图识别,直接进规划流水线",
    )
    session_id: Optional[str] = Field(
        default=None,
        description="服务端会话 ID。用于读取上一版行程并支持会话内修改",
        max_length=128,
    )
    user_id: Optional[str] = Field(
        default=None,
        description="稳定用户 ID。多用户部署时应来自鉴权层",
        max_length=128,
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "message": "帮我规划北京三日游,下周末出发,带个六岁小孩",
                "history": [],
            }
        }
    )

