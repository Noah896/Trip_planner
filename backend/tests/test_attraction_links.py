"""景点详情链接所需的 POI ID 回填测试。"""

from app.agents.trip_planner_agent import MultiAgentTripPlanner
from app.models.schemas import Attraction, DayPlan, Location, TripPlan


def _attraction(name: str) -> Attraction:
    return Attraction(
        name=name,
        address="测试地址",
        location=Location(longitude=113.0, latitude=28.0),
        visit_duration=120,
        description="测试描述",
    )


def test_fill_attraction_poi_ids_from_retrieval_text():
    plan = TripPlan(
        city="长沙",
        start_date="2026-11-01",
        end_date="2026-11-01",
        days=[
            DayPlan(
                date="2026-11-01",
                day_index=0,
                description="第一天",
                transportation="公共交通",
                accommodation="经济型酒店",
                attractions=[
                    _attraction("湖南省植物园"),
                    _attraction("洋湖国家湿地公园"),
                ],
            )
        ],
        overall_suggestions="测试",
    )
    attractions_text = "\n".join(
        [
            (
                "1. 湖南省植物园 / 植物园路111号 / 经纬度 113.02,28.11 / "
                "POI ID B000PLANT"
            ),
            (
                "2. 洋湖国家湿地公园 / 洋湖大道680号 / 经纬度 112.95,28.08 / "
                "POI ID B000WETLAND"
            ),
        ]
    )

    filled = MultiAgentTripPlanner._fill_attraction_poi_ids(plan, attractions_text)

    assert [item.poi_id for item in filled.days[0].attractions] == [
        "B000PLANT",
        "B000WETLAND",
    ]


def test_fill_attraction_poi_ids_keeps_model_value_and_does_not_guess():
    plan = TripPlan(
        city="长沙",
        start_date="2026-11-01",
        end_date="2026-11-01",
        days=[
            DayPlan(
                date="2026-11-01",
                day_index=0,
                description="第一天",
                transportation="公共交通",
                accommodation="经济型酒店",
                attractions=[
                    _attraction("湖南省植物园").model_copy(
                        update={"poi_id": "FROM_MODEL"}
                    ),
                    _attraction("完全不匹配的景点"),
                ],
            )
        ],
        overall_suggestions="测试",
    )
    attractions_text = (
        "1. 湖南省植物园 / 植物园路111号 / 经纬度 113.02,28.11 / "
        "POI ID B000PLANT"
    )

    filled = MultiAgentTripPlanner._fill_attraction_poi_ids(plan, attractions_text)

    assert filled.days[0].attractions[0].poi_id == "FROM_MODEL"
    assert filled.days[0].attractions[1].poi_id in {None, ""}
