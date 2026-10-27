"""用户输入“省份 + 城市”时的目的地规范化测试。"""

from __future__ import annotations

import asyncio

import pytest

from app.models.schemas import RouteRequest, TripRequest
from app.services.amap_service import AmapService
from app.services.chat_service import ChatDecision
from app.utils.city_normalization import normalize_city_name


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("湖南郴州", "郴州"),
        ("中国湖南郴州", "郴州"),
        ("湖南省郴州市", "郴州"),
        ("湖南-郴州", "郴州"),
        (" 湖南省 / 郴州市 ", "郴州"),
        ("湖南省郴州市北湖区", "郴州"),
        ("广东深圳", "深圳"),
        ("广西壮族自治区", "广西"),
        ("湖南省", "湖南"),
        ("广西壮族自治区桂林市", "桂林"),
        ("内蒙古自治区呼和浩特市", "呼和浩特"),
        ("新疆维吾尔自治区乌鲁木齐市", "乌鲁木齐"),
        ("宁夏回族自治区银川市", "银川"),
        ("北京市朝阳区", "北京"),
        ("上海市浦东新区", "上海"),
        ("重庆渝中区", "重庆"),
        ("海南三亚", "三亚"),
        ("海南省三亚市", "三亚"),
        ("海南藏族自治州", "海南藏族自治州"),
        ("河北区", "河北区"),
        ("吉林", "吉林"),
        ("吉林省吉林市", "吉林"),
        ("湘西土家族苗族自治州", "湘西土家族苗族自治州"),
        ("香港特别行政区", "香港"),
        ("澳门", "澳门"),
        ("郴州市", "郴州"),
        (" 成都 ", "成都"),
    ],
)
def test_normalize_city_name(raw, expected):
    assert normalize_city_name(raw) == expected


def test_trip_and_route_models_normalize_city():
    trip = TripRequest(
        city="湖南郴州",
        start_date="2026-10-28",
        end_date="2026-10-29",
        travel_days=2,
        transportation="公共交通",
        accommodation="经济型酒店",
    )
    route = RouteRequest(
        origin_address="甲",
        destination_address="乙",
        origin_city="广东省深圳市",
        destination_city="湖南郴州",
    )

    assert trip.city == "郴州"
    assert route.origin_city == "深圳"
    assert route.destination_city == "郴州"
    assert (
        ChatDecision.model_validate(
            {"intent": "plan", "city": "湖南郴州"}
        ).city
        == "郴州"
    )


def test_amap_service_normalizes_city_before_tool_call(monkeypatch):
    service = AmapService()
    calls = []

    async def _call(tool_name, arguments):
        calls.append((tool_name, arguments))
        return {"pois": []}

    monkeypatch.setattr(service, "_call", _call)

    asyncio.run(service.search_poi("景点", "湖南郴州"))
    asyncio.run(service.get_weather("湖南省郴州市"))

    assert calls[0][1]["city"] == "郴州"
    assert calls[1][1]["city"] == "郴州"
