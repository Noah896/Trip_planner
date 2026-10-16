"""结构化输出的容错解析测试(离线)

对应 2026-10-16 定位到的端到端阻塞根因:模型把嵌套对象写成 **JSON 字符串**
(实测 ``"budget": "{\\"total\\": 1140}"``)。Pydantic 报 model_type 错误后,
LangChain 会回灌错误让模型重试,而模型把一模一样的参数再发一遍 —— 于是
循环到 recursion_limit 才停,端到端表现为 "/plan 11 分钟不返回"。

这些用例锁住的就是"这类畸形输出必须能被吃下去"。
"""

import json

import pytest

from app.models.schemas import Attraction, DayPlan, Location, TripPlan


def _day_dict(**overrides):
    day = {
        "date": "2025-06-01",
        "day_index": 0,
        "description": "第1天",
        "transportation": "公共交通",
        "accommodation": "经济型酒店",
        "hotel": {
            "name": "前门建国饭店",
            "location": {"longitude": 116.39, "latitude": 39.895},
        },
        "attractions": [
            {
                "name": "故宫博物院",
                "address": "景山前街4号",
                "location": {"longitude": 116.397, "latitude": 39.918},
                "visit_duration": 180,
                "description": "明清皇宫",
                "ticket_price": 60,
            }
        ],
        "meals": [
            {
                "type": "lunch",
                "name": "烤鸭",
                "location": {"longitude": 116.4, "latitude": 39.9},
            }
        ],
    }
    day.update(overrides)
    return day


class TestBudgetAsJsonString:
    """本次踩到的那个坑:budget 被当成字符串返回。"""

    def test_budget_json_string_is_parsed(self):
        budget = {"total_attractions": 105, "total_hotels": 700, "total": 1140}
        plan = TripPlan.model_validate(
            {
                "city": "北京",
                "start_date": "2025-06-01",
                "end_date": "2025-06-01",
                "days": [_day_dict()],
                "weather_info": [],
                "overall_suggestions": "注意防晒",
                # 模型实际发过来的样子
                "budget": json.dumps(budget, ensure_ascii=False),
            }
        )

        assert plan.budget is not None
        assert plan.budget.total == 1140

    def test_budget_object_still_works(self):
        """正常路径不能被容错逻辑带坏。"""
        plan = TripPlan.model_validate(
            {
                "city": "北京",
                "start_date": "2025-06-01",
                "end_date": "2025-06-01",
                "days": [],
                "overall_suggestions": "x",
                "budget": {"total": 100},
            }
        )
        assert plan.budget.total == 100

    def test_broken_budget_string_still_fails(self):
        """不是 JSON 的字符串不能被悄悄吞掉,该报错还得报错。"""
        with pytest.raises(Exception):
            TripPlan.model_validate(
                {
                    "city": "北京",
                    "start_date": "2025-06-01",
                    "end_date": "2025-06-01",
                    "days": [],
                    "overall_suggestions": "x",
                    "budget": "一千块左右",
                }
            )


class TestNestedContainersAsJsonString:
    def test_days_json_string_is_parsed(self):
        plan = TripPlan.model_validate(
            {
                "city": "北京",
                "start_date": "2025-06-01",
                "end_date": "2025-06-01",
                "days": json.dumps([_day_dict()], ensure_ascii=False),
                "weather_info": json.dumps(
                    [{"date": "2025-06-01", "day_weather": "晴", "day_temp": 28}],
                    ensure_ascii=False,
                ),
                "overall_suggestions": "x",
            }
        )
        assert len(plan.days) == 1
        assert plan.weather_info[0].day_temp == 28

    def test_day_level_containers_as_json_string(self):
        day = DayPlan.model_validate(
            _day_dict(
                attractions=json.dumps(_day_dict()["attractions"], ensure_ascii=False),
                meals=json.dumps(_day_dict()["meals"], ensure_ascii=False),
                hotel=json.dumps(_day_dict()["hotel"], ensure_ascii=False),
            )
        )
        assert day.attractions[0].name == "故宫博物院"
        assert day.meals[0].name == "烤鸭"
        assert day.hotel.name == "前门建国饭店"

    def test_real_dicts_are_untouched(self):
        day = DayPlan.model_validate(_day_dict())
        assert day.attractions[0].location.longitude == 116.397
        assert day.attractions[0].ticket_price == 60


class TestLocationStringForms:
    """坐标写成 ``"lng,lat"`` 或 JSON 字符串时也要能收下。"""

    def test_comma_string(self):
        location = Location.model_validate("116.397128,39.916527")
        assert (location.longitude, location.latitude) == (116.397128, 39.916527)

    def test_json_string(self):
        location = Location.model_validate('{"longitude": 116.4, "latitude": 39.9}')
        assert (location.longitude, location.latitude) == (116.4, 39.9)

    def test_attraction_accepts_comma_string(self):
        attraction = Attraction.model_validate(
            {
                "name": "天坛公园",
                "address": "天坛东里甲1号",
                "location": "116.412100,39.882000",
                "visit_duration": 120,
                "description": "祭天场所",
            }
        )
        assert attraction.location.longitude == 116.412100

    def test_garbage_still_fails(self):
        with pytest.raises(Exception):
            Location.model_validate({"longitude": "东经116", "latitude": 39.9})
