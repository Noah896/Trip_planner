"""行程质量规则的离线测试。"""

from app.agents.trip_planner_agent import MultiAgentTripPlanner
from app.models.schemas import Location, TripRequest
from app.services import itinerary_quality


BJ_CENTER = Location(longitude=116.4074, latitude=39.9042)


def _request(**overrides) -> TripRequest:
    payload = {
        "city": "北京",
        "start_date": "2026-11-21",
        "end_date": "2026-11-23",
        "travel_days": 3,
        "transportation": "公共交通",
        "accommodation": "舒适型酒店",
        "preferences": ["历史文化"],
        "free_text_input": "",
    }
    payload.update(overrides)
    return TripRequest(**payload)


def test_haversine_distance_is_reasonable():
    distance = itinerary_quality.haversine_km(
        BJ_CENTER,
        {"longitude": 116.397, "latitude": 39.918},
    )

    assert distance is not None
    assert 1.0 < distance < 2.5
    assert itinerary_quality.haversine_km(BJ_CENTER, "未知") is None


def test_compact_request_matches_family_and_relaxed_language():
    assert itinerary_quality.is_compact_request(
        _request(preferences=["亲子", "休闲"])
    )
    assert itinerary_quality.is_compact_request(
        _request(free_text_input="带老人,少走路")
    )
    assert not itinerary_quality.is_compact_request(
        _request(preferences=["历史文化"], free_text_input="想看博物馆")
    )


def test_compact_ranking_moves_near_museum_before_far_scenic_spot():
    items = [
        {
            "name": "百里山水画廊",
            "type": "110202",
            "location": "116.75,40.55",
        },
        {
            "name": "首都博物馆",
            "type": "140200",
            "location": "116.34,39.91",
        },
        {
            "name": "天坛公园",
            "type": "110202",
            "location": "116.410,39.882",
        },
    ]

    ranked = itinerary_quality.rank_attraction_candidates(
        items,
        BJ_CENTER,
        compact=True,
    )

    assert [item["name"] for item in ranked[:2]] == ["天坛公园", "首都博物馆"]
    assert all("center_distance_km" in item for item in ranked)


def test_normal_ranking_keeps_scenic_spot_relevance_first():
    items = [
        {
            "name": "首都博物馆",
            "type": "140200",
            "location": "116.34,39.91",
        },
        {
            "name": "百里山水画廊",
            "type": "110202",
            "location": "116.75,40.55",
        },
    ]

    ranked = itinerary_quality.rank_attraction_candidates(
        items,
        BJ_CENTER,
        compact=False,
    )

    assert ranked[0]["name"] == "百里山水画廊"


def test_compact_retention_drops_remote_candidates_when_enough_local_items():
    ranked = itinerary_quality.rank_attraction_candidates(
        [
            {
                "name": "南苑森林湿地公园",
                "type": "110202",
                "location": "116.378504,39.820418",
            },
            {
                "name": "北坞公园",
                "type": "110202",
                "location": "116.256600,39.986214",
            },
            {
                "name": "千灵山",
                "type": "110202",
                "location": "116.085523,39.860994",
            },
            {
                "name": "北京金海湖风景区",
                "type": "110202",
                "location": "117.301097,40.180720",
            },
        ],
        BJ_CENTER,
        compact=True,
    )

    retained = itinerary_quality.retain_compact_candidates(
        ranked,
        min_candidates=3,
    )

    assert [item["name"] for item in retained] == [
        "南苑森林湿地公园",
        "北坞公园",
        "千灵山",
    ]


def test_compact_retention_keeps_nearest_remote_items_when_local_pool_is_small():
    ranked = [
        {"name": "近处", "center_distance_km": 20.0},
        {"name": "较远", "center_distance_km": 40.0},
        {"name": "最远", "center_distance_km": 80.0},
    ]

    retained = itinerary_quality.retain_compact_candidates(
        ranked,
        min_candidates=3,
    )

    assert [item["name"] for item in retained] == ["近处", "较远", "最远"]


def test_daily_route_span_uses_visit_order():
    attractions = [
        {"location": {"longitude": 116.397, "latitude": 39.918}},
        {"location": {"longitude": 116.396, "latitude": 39.925}},
        {"location": {"longitude": 116.410, "latitude": 39.882}},
    ]

    span = itinerary_quality.daily_route_span_km(attractions)

    assert span is not None
    assert 4.5 < span < 6.5


def test_planner_prompt_adds_compact_constraints_only_when_needed():
    planner = MultiAgentTripPlanner(llm=object())

    compact_query = planner._build_planner_query(
        _request(preferences=["亲子"], free_text_input="少走路"),
        "1. 天坛公园 / 距城市中心 3.0 公里",
        "2026-11-21 / 晴",
    )
    normal_query = planner._build_planner_query(
        _request(preferences=["历史文化"]),
        "1. 天坛公园",
        "2026-11-21 / 晴",
    )

    assert "路线舒适度约束" in compact_query
    assert "每天最多安排一个明显远离市区的景点" in compact_query
    assert "路线舒适度约束" not in normal_query
