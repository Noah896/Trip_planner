"""确定性三路检索的测试(离线,不联网、不花额度)

对应 2026-10-16 的第二轮优化:三路检索从"各挂一个 LLM Agent"改成服务层
确定性调用 —— 检索占掉了 8~9 次模型调用,而它本身并不需要模型判断。
这里用假的高德服务锁住几个关键行为:

    - 多关键词结果按 id 去重;
    - 坐标由 `maps_search_detail` 真实回填(而不是让模型编);
    - 风景名胜排在餐饮之类的噪音前面;
    - 回填次数有上界(高德 detail 是串行的,次数直接等于耗时)。
"""

import asyncio

from app.models.schemas import Location, POIInfo, TripRequest, WeatherInfo
from app.services import trip_retrieval


class _FakeService:
    """按关键词返回预设 POI 的假高德服务。"""

    def __init__(self, pois_by_keyword=None, details=None, weather=None):
        self.pois_by_keyword = pois_by_keyword or {}
        self.details = details or {}
        self.weather = weather or []
        self.searched_keywords = []
        self.detail_calls = []

    async def search_poi(self, keywords, city, citylimit=True):
        self.searched_keywords.append(keywords)
        return [POIInfo(**poi) for poi in self.pois_by_keyword.get(keywords, [])]

    async def get_poi_detail(self, poi_id):
        self.detail_calls.append(poi_id)
        return self.details.get(poi_id, {})

    async def get_weather(self, city):
        return self.weather


def _request(**overrides) -> TripRequest:
    payload = {
        "city": "北京",
        "start_date": "2025-06-01",
        "end_date": "2025-06-02",
        "travel_days": 2,
        "transportation": "公共交通",
        "accommodation": "经济型酒店",
        "preferences": ["历史文化"],
    }
    payload.update(overrides)
    return TripRequest(**payload)


class TestDetailBudget:
    def test_scales_with_days_and_is_clamped(self):
        # 2 天 → 最少 6 个
        assert trip_retrieval._detail_budget(2, 6, 12) == 6
        # 10 天 → 触到上限
        assert trip_retrieval._detail_budget(10, 6, 12) == 12
        # 0 天(异常输入)→ 仍然给最小值,不至于检索不到东西
        assert trip_retrieval._detail_budget(0, 6, 12) == 6


class TestCollectPois:
    def test_dedupes_across_keywords_and_keeps_order(self):
        service = _FakeService(
            pois_by_keyword={
                "历史文化": [
                    {"id": "B1", "name": "故宫博物院", "type": "110202", "address": "景山前街4号"},
                    {"id": "B2", "name": "八达岭长城", "type": "110202", "address": "G6京藏高速"},
                ],
                "美食": [
                    {"id": "B2", "name": "八达岭长城", "type": "110202", "address": "G6京藏高速"},
                    {"id": "B3", "name": "全聚德", "type": "050100", "address": "前门大街"},
                ],
            }
        )

        pois = asyncio.run(
            trip_retrieval._collect_pois(service, ["历史文化", "美食"], "北京")
        )

        assert [poi.id for poi in pois] == ["B1", "B2", "B3"]

    def test_skips_pois_without_id(self):
        service = _FakeService(
            pois_by_keyword={"景点": [{"id": "", "name": "没有 id", "type": "110000", "address": "某地"}]}
        )
        assert asyncio.run(trip_retrieval._collect_pois(service, ["景点"], "北京")) == []


class TestRetrieveAttractions:
    def test_backfills_real_coordinates(self, monkeypatch):
        service = _FakeService(
            pois_by_keyword={
                "历史文化": [
                    {
                        "id": "B1",
                        "name": "故宫博物院",
                        "type": "110202",
                        "address": "景山前街4号",
                    }
                ]
            },
            details={
                "B1": {
                    "location": "116.397029,39.917839",
                    "rating": "4.9",
                    "opentime2": "08:30-17:00",
                }
            },
        )
        monkeypatch.setattr(trip_retrieval, "get_amap_service", lambda: service)

        text = asyncio.run(trip_retrieval.retrieve_attractions(_request()))

        assert "故宫博物院" in text
        # 坐标来自 maps_search_detail,而不是模型编的
        assert "116.397029,39.917839" in text
        assert "评分 4.9" in text
        assert service.detail_calls == ["B1"]

    def test_scenic_spots_rank_before_restaurants(self, monkeypatch):
        """用"美食"搜索时也会搜出饭馆,不能让它们挤掉景点。"""
        service = _FakeService(
            pois_by_keyword={
                "美食": [
                    {"id": "R1", "name": "全聚德", "type": "050100", "address": "前门大街"},
                    {"id": "S1", "name": "天坛公园", "type": "110202", "address": "天坛东里"},
                ]
            }
        )
        monkeypatch.setattr(trip_retrieval, "get_amap_service", lambda: service)

        text = asyncio.run(
            trip_retrieval.retrieve_attractions(_request(preferences=["美食"]))
        )

        assert text.index("天坛公园") < text.index("全聚德")

    def test_compact_route_ranks_near_candidates_before_far_scenic_spot(
        self, monkeypatch
    ):
        class _Centered(_FakeService):
            async def geocode(self, address, city=None):
                return Location(longitude=116.4074, latitude=39.9042)

        service = _Centered(
            pois_by_keyword={
                "亲子": [
                    {
                        "id": "F1",
                        "name": "百里山水画廊",
                        "type": "110202",
                        "address": "延庆区千家店镇",
                    },
                    {
                        "id": "M1",
                        "name": "首都博物馆",
                        "type": "140200",
                        "address": "复兴门外大街16号",
                    },
                ]
            },
            details={
                "F1": {"location": "116.75,40.55"},
                "M1": {"location": "116.34,39.91"},
            },
        )
        monkeypatch.setattr(trip_retrieval, "get_amap_service", lambda: service)

        text = asyncio.run(
            trip_retrieval.retrieve_attractions(
                _request(
                    preferences=["亲子", "休闲"],
                    free_text_input="带六岁小孩,少走路",
                )
            )
        )

        assert text.index("首都博物馆") < text.index("百里山水画廊")
        assert "距城市中心" in text

    def test_compact_route_removes_far_candidates_when_local_pool_is_enough(
        self, monkeypatch
    ):
        class _Centered(_FakeService):
            async def geocode(self, address, city=None):
                return Location(longitude=116.4074, latitude=39.9042)

        service = _Centered(
            pois_by_keyword={
                "亲子": [
                    {
                        "id": "L1",
                        "name": "南苑森林湿地公园",
                        "type": "110202",
                        "address": "槐房路42号",
                    },
                    {
                        "id": "L2",
                        "name": "北坞公园",
                        "type": "110202",
                        "address": "颐和园西门外",
                    },
                    {
                        "id": "L3",
                        "name": "千灵山",
                        "type": "110202",
                        "address": "王佐镇西庄店村北",
                    },
                    {
                        "id": "F1",
                        "name": "北京金海湖风景区",
                        "type": "110202",
                        "address": "金海湖镇坝前广场路1号",
                    },
                ]
            },
            details={
                "L1": {"location": "116.378504,39.820418"},
                "L2": {"location": "116.256600,39.986214"},
                "L3": {"location": "116.085523,39.860994"},
                "F1": {"location": "117.301097,40.180720"},
            },
        )
        monkeypatch.setattr(trip_retrieval, "get_amap_service", lambda: service)

        text = asyncio.run(
            trip_retrieval.retrieve_attractions(
                _request(
                    preferences=["亲子", "休闲"],
                    free_text_input="带六岁小孩,少走路",
                )
            )
        )

        assert "南苑森林湿地公园" in text
        assert "北京金海湖风景区" not in text

    def test_falls_back_to_generic_keyword(self, monkeypatch):
        service = _FakeService()
        monkeypatch.setattr(trip_retrieval, "get_amap_service", lambda: service)

        asyncio.run(trip_retrieval.retrieve_attractions(_request(preferences=[])))

        assert service.searched_keywords == ["景点"]

    def test_search_failure_returns_empty_instead_of_raising(self, monkeypatch):
        class _Boom(_FakeService):
            async def search_poi(self, keywords, city, citylimit=True):
                raise RuntimeError("高德挂了")

        monkeypatch.setattr(trip_retrieval, "get_amap_service", _Boom)

        # 返回空串,由 plan_trip_with_status 统一决定是否降级
        assert asyncio.run(trip_retrieval.retrieve_attractions(_request())) == ""


class TestMustVisit:
    """用户点名的景点必须是硬约束,而不是一句自由文本。"""

    def test_pinned_poi_goes_first_even_if_preferences_point_elsewhere(self, monkeypatch):
        service = _FakeService(
            pois_by_keyword={
                # 用户点名的那个
                "故宫博物院": [
                    {"id": "P1", "name": "故宫博物院", "type": "110202", "address": "景山前街4号"}
                ],
                # 偏好词搜出来的一堆
                "历史文化": [
                    {"id": "S1", "name": "天坛公园", "type": "110202", "address": "天坛东里"},
                    {"id": "S2", "name": "颐和园", "type": "110202", "address": "新建宫门路"},
                ],
            },
            details={
                "P1": {"location": "116.397029,39.917839"},
                "S1": {"location": "116.4121,39.882"},
                "S2": {"location": "116.275,39.999"},
            },
        )
        monkeypatch.setattr(trip_retrieval, "get_amap_service", lambda: service)

        text = asyncio.run(
            trip_retrieval.retrieve_attractions(
                _request(preferences=["历史文化"], must_visit=["故宫博物院"])
            )
        )

        # 第一条就是用户点名要的那个
        assert text.splitlines()[0].startswith("1. 故宫博物院")
        # 而且真的回填了它的坐标
        assert "116.397029,39.917839" in text

    def test_not_duplicated_when_it_also_matches_preferences(self, monkeypatch):
        service = _FakeService(
            pois_by_keyword={
                "故宫博物院": [
                    {"id": "P1", "name": "故宫博物院", "type": "110202", "address": "景山前街4号"}
                ],
                "历史文化": [
                    {"id": "P1", "name": "故宫博物院", "type": "110202", "address": "景山前街4号"}
                ],
            },
            details={"P1": {"location": "116.397029,39.917839"}},
        )
        monkeypatch.setattr(trip_retrieval, "get_amap_service", lambda: service)

        text = asyncio.run(
            trip_retrieval.retrieve_attractions(
                _request(preferences=["历史文化"], must_visit=["故宫博物院"])
            )
        )

        assert text.count("故宫博物院") == 1
        assert service.detail_calls == ["P1"]

    def test_falls_back_to_geocode_when_name_search_finds_nothing(self, monkeypatch):
        """用户说的是口语简称("故宫")时,搜不到 POI 也要把坐标拿到。"""

        class _Geo(_FakeService):
            async def geocode(self, address, city=None):
                from app.models.schemas import Location

                if address == "故宫":
                    return Location(longitude=116.397029, latitude=39.917839)
                return None

        service = _Geo(pois_by_keyword={})
        monkeypatch.setattr(trip_retrieval, "get_amap_service", lambda: service)

        text = asyncio.run(
            trip_retrieval.retrieve_attractions(_request(must_visit=["故宫"]))
        )

        assert "故宫" in text
        assert "116.397029,39.917839" in text
        # 没有 id 的候选不该去调 detail(会白白打一次接口)
        assert service.detail_calls == []

    def test_without_must_visit_nothing_changes(self, monkeypatch):
        service = _FakeService(
            pois_by_keyword={
                "历史文化": [
                    {"id": "S1", "name": "天坛公园", "type": "110202", "address": "天坛东里"}
                ]
            },
            details={"S1": {"location": "116.4121,39.882"}},
        )
        monkeypatch.setattr(trip_retrieval, "get_amap_service", lambda: service)

        text = asyncio.run(trip_retrieval.retrieve_attractions(_request()))

        assert "天坛公园" in text
        assert service.searched_keywords == ["历史文化"]


class TestRetrieveWeather:
    def test_renders_expected_field_order(self, monkeypatch):
        weather = [
            WeatherInfo(
                date="2025-06-01",
                day_weather="晴",
                night_weather="多云",
                day_temp=28,
                night_temp=18,
                wind_direction="南风",
                wind_power="1-3级",
            )
        ]
        service = _FakeService(weather=weather)
        monkeypatch.setattr(trip_retrieval, "get_amap_service", lambda: service)

        text = asyncio.run(trip_retrieval.retrieve_weather(_request()))

        # 规划提示词要求的顺序:日期 / 白天 / 夜间 / 白天温度 / 夜间温度 / 风向 / 风力
        assert text == "2025-06-01 / 晴 / 多云 / 28 / 18 / 南风 / 1-3级"

    def test_no_weather_returns_empty(self, monkeypatch):
        monkeypatch.setattr(trip_retrieval, "get_amap_service", lambda: _FakeService())
        assert asyncio.run(trip_retrieval.retrieve_weather(_request())) == ""


class TestRetrieveHotels:
    def test_searches_accommodation_preference_and_generic_keyword(self, monkeypatch):
        service = _FakeService(
            pois_by_keyword={
                "经济型酒店": [
                    {
                        "id": "H1",
                        "name": "如家酒店(前门店)",
                        "type": "100000",
                        "address": "大栅栏西街",
                    }
                ]
            },
            details={"H1": {"location": "116.395,39.893", "rating": "4.1"}},
        )
        monkeypatch.setattr(trip_retrieval, "get_amap_service", lambda: service)

        text = asyncio.run(trip_retrieval.retrieve_hotels(_request()))

        assert "如家酒店(前门店)" in text
        assert "116.395,39.893" in text
        assert service.searched_keywords == ["经济型酒店", "酒店"]


class TestSmallHelpers:
    def test_location_text(self):
        assert trip_retrieval._location_text("116.4,39.9") == "116.4,39.9"
        assert trip_retrieval._location_text({"lng": 116.4, "lat": 39.9}) == "116.4,39.9"
        assert trip_retrieval._location_text(None) == "未知"

    def test_cost_text(self):
        cost = [{"ticket_type": "成人票", "price": 60}]
        assert trip_retrieval._cost_text(cost) == "成人票 60元"
        assert trip_retrieval._cost_text([]) == ""
        assert trip_retrieval._cost_text(None) == ""

    def test_render_skips_empty_fields(self):
        text = trip_retrieval._render_attractions(
            [
                {
                    "name": "天坛公园",
                    "address": "",
                    "location": "116.4121,39.882",
                    "type": "",
                    "rating": "",
                    "cost": "",
                    "opentime": "",
                }
            ]
        )
        assert text == "1. 天坛公园 / 经纬度 116.4121,39.882"

    def test_render_includes_poi_id_for_detail_link(self):
        text = trip_retrieval._render_attractions(
            [
                {
                    "name": "湖南省植物园",
                    "address": "植物园路111号",
                    "location": "113.02,28.11",
                    "type": "风景名胜",
                    "rating": "4.7",
                    "cost": "",
                    "opentime": "",
                    "poi_id": "B000TEST",
                }
            ]
        )

        assert "POI ID B000TEST" in text
