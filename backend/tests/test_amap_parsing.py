"""高德 MCP 返回值的解析测试

``AmapService`` 的解析方法都是纯函数(不发起网络请求),
这里直接构造高德真实返回结构来验证,避免"改解析逻辑没有任何测试兜底"。
"""

import asyncio

from app.services.amap_service import AmapService


class TestParseLocation:
    def test_parses_dict_with_long_lat(self):
        loc = AmapService._parse_location({"longitude": 116.397, "latitude": 39.918})
        assert (loc.longitude, loc.latitude) == (116.397, 39.918)

    def test_parses_dict_with_lng_lat(self):
        loc = AmapService._parse_location({"lng": "116.4", "lat": "39.9"})
        assert (loc.longitude, loc.latitude) == (116.4, 39.9)

    def test_parses_comma_string(self):
        loc = AmapService._parse_location("116.397128,39.916527")
        assert (loc.longitude, loc.latitude) == (116.397128, 39.916527)

    def test_returns_none_on_bad_input(self):
        assert AmapService._parse_location("没有逗号") is None
        assert AmapService._parse_location({"longitude": "abc", "latitude": 1}) is None
        assert AmapService._parse_location(None) is None


class TestParsePois:
    def test_parses_pois_list(self):
        data = {
            "pois": [
                {
                    "id": "B000A8UIN8",
                    "name": "故宫博物院",
                    "typecode": "110101",
                    "address": "景山前街4号",
                    "location": "116.397128,39.916527",
                }
            ]
        }
        pois = AmapService._parse_pois(data)
        assert len(pois) == 1
        assert pois[0].name == "故宫博物院"
        assert pois[0].type == "110101"
        assert pois[0].location.longitude == 116.397128

    def test_falls_back_to_data_key(self):
        pois = AmapService._parse_pois({"data": [{"id": "1", "name": "外滩"}]})
        assert [p.name for p in pois] == ["外滩"]

    def test_keeps_poi_without_location(self):
        """maps_text_search 不返回坐标,不能因此丢弃 POI。"""
        pois = AmapService._parse_pois({"pois": [{"id": "1", "name": "西湖"}]})
        assert pois[0].location is None

    def test_skips_non_dict_items(self):
        pois = AmapService._parse_pois({"pois": ["坏数据", {"id": "1", "name": "岳麓山"}]})
        assert [p.name for p in pois] == ["岳麓山"]

    def test_returns_empty_on_unexpected_shape(self):
        assert AmapService._parse_pois(None) == []
        assert AmapService._parse_pois("字符串") == []
        assert AmapService._parse_pois({}) == []


class TestGeocode:
    def test_parses_project_mcp_return_shape(self, monkeypatch):
        service = AmapService()

        async def fake_call(tool_name, arguments):
            assert tool_name == "maps_geo"
            return {
                "return": [
                    {
                        "province": "北京市",
                        "city": "北京市",
                        "location": "116.397463,39.909187",
                    }
                ]
            }

        monkeypatch.setattr(service, "_call", fake_call)
        location = asyncio.run(service.geocode("北京", city="北京"))

        assert location is not None
        assert location.longitude == 116.397463
        assert location.latitude == 39.909187

    def test_parses_open_platform_geocodes_shape(self, monkeypatch):
        service = AmapService()

        async def fake_call(tool_name, arguments):
            return {"geocodes": [{"location": "121.473701,31.230416"}]}

        monkeypatch.setattr(service, "_call", fake_call)
        location = asyncio.run(service.geocode("上海"))

        assert location is not None
        assert location.longitude == 121.473701


class TestParseWeather:
    def test_parses_lives(self):
        data = {
            "lives": [
                {
                    "date": "2025-06-01",
                    "dayweather": "晴",
                    "nightweather": "多云",
                    "daytemp": "25",
                    "nighttemp": "15",
                    "daywind": "南风",
                    "daypower": "1-3级",
                }
            ]
        }
        weather = AmapService._parse_weather(data)
        assert len(weather) == 1
        assert weather[0].day_weather == "晴"
        assert weather[0].day_temp == 25
        assert weather[0].wind_power == "1-3级"

    def test_parses_forecasts_with_nested_casts(self):
        """高德开放平台原始结构:forecasts[].casts[]。"""
        data = {
            "forecasts": [
                {
                    "city": "北京",
                    "casts": [
                        {"date": "2025-06-01", "dayweather": "晴", "daytemp": "30"},
                        {"date": "2025-06-02", "dayweather": "阴", "daytemp": "28"},
                    ],
                }
            ]
        }
        weather = AmapService._parse_weather(data)
        assert [w.date for w in weather] == ["2025-06-01", "2025-06-02"]
        assert weather[1].day_temp == 28

    def test_parses_forecasts_holding_days_directly(self):
        """本项目所用 MCP Server 的实际返回:forecasts[] 下直接是每日天气。"""
        data = {"forecasts": [{"date": "2025-06-01", "weather": "小雨", "temperature": "22"}]}
        weather = AmapService._parse_weather(data)
        assert len(weather) == 1
        assert weather[0].day_weather == "小雨"
        assert weather[0].night_weather == "小雨"
        assert weather[0].day_temp == 22

    def test_strips_temperature_unit(self):
        data = {"lives": [{"date": "2025-06-01", "daytemp": "25°C", "nighttemp": "15℃"}]}
        weather = AmapService._parse_weather(data)
        assert weather[0].day_temp == 25
        assert weather[0].night_temp == 15

    def test_returns_empty_on_unexpected_shape(self):
        assert AmapService._parse_weather(None) == []
        assert AmapService._parse_weather({}) == []


class TestNormalizeRoute:
    def test_parses_paths(self):
        data = {"route": {"paths": [{"distance": "1200", "duration": "900"}]}}
        route = AmapService._normalize_route(data, "walking", "A", "B")
        assert route["distance"] == 1200.0
        assert route["duration"] == 900
        assert "1.2 公里" in route["description"]
        assert route["route_type"] == "walking"

    def test_parses_transits(self):
        data = {"route": {"transits": [{"distance": "5000", "duration": "1800"}]}}
        route = AmapService._normalize_route(data, "transit", "A", "B")
        assert route["distance"] == 5000.0
        assert "30 分钟" in route["description"]

    def test_degrades_to_zero_instead_of_raising(self):
        """解析不出距离/耗时也必须返回合法结构,不能让路由 500。"""
        route = AmapService._normalize_route(
            {"info": "INVALID_USER_KEY"}, "driving", "A", "B"
        )
        assert route["distance"] == 0
        assert route["duration"] == 0
        assert "INVALID_USER_KEY" in route["description"]
        assert route["raw"] == {"info": "INVALID_USER_KEY"}

    def test_handles_none_payload(self):
        route = AmapService._normalize_route(None, "walking", "A", "B")
        assert route["distance"] == 0
        assert route["duration"] == 0
