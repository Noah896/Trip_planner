"""旅行计划质量评测器。

评测器不调用模型,只检查已经拿到的响应。live 运行由脚本负责。
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from app.models.schemas import TripPlan, TripRequest
from app.services.itinerary_quality import daily_route_span_km, haversine_km


DEFAULT_CASES_PATH = Path(__file__).with_name("golden_cases.json")
DEFAULT_MAX_LATENCY_SECONDS = 180.0
MUST_VISIT_ALIAS_GROUPS = (
    ("兵马俑", "秦始皇帝陵博物院", "秦始皇兵马俑博物馆"),
)


def load_golden_cases(path: str | Path = DEFAULT_CASES_PATH) -> List[Dict[str, Any]]:
    """读取并校验 golden cases。"""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("golden cases 必须是非空数组")

    seen = set()
    result = []
    for raw in cases:
        case_id = str(raw.get("id") or "").strip()
        if not case_id or case_id in seen:
            raise ValueError(f"golden case id 缺失或重复: {case_id!r}")
        seen.add(case_id)
        request = TripRequest.model_validate(raw.get("request") or {})
        expect = raw.get("expect") or {}
        result.append(
            {
                **raw,
                "id": case_id,
                "request": request.model_dump(mode="json"),
                "expect": expect,
            }
        )
    return result


def _check(
    name: str,
    passed: bool,
    detail: str,
    *,
    weight: int,
) -> Dict[str, Any]:
    return {
        "name": name,
        "passed": bool(passed),
        "detail": detail,
        "weight": weight,
    }


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", "", (value or "").casefold())


def _contains_name(actual: str, expected: str) -> bool:
    left = _normalize_text(actual)
    right = _normalize_text(expected)
    return bool(left and right and (left in right or right in left))


def _collect_attractions(plan: TripPlan):
    return [attraction for day in plan.days for attraction in day.attractions]


def _must_visit_names(
    request: TripRequest,
    expect: Dict[str, Any],
) -> List[str]:
    names: List[str] = []
    for value in [
        *request.must_visit,
        *(expect.get("must_visit") or []),
    ]:
        normalized = str(value).strip()
        if normalized and normalized not in names:
            names.append(normalized)
    return names


def _matches_must_visit(name: str, must_visit: List[str]) -> bool:
    actual = _normalize_text(name)
    for expected in must_visit:
        if _contains_name(name, expected):
            return True
        wanted = _normalize_text(expected)
        for group in MUST_VISIT_ALIAS_GROUPS:
            aliases = [_normalize_text(alias) for alias in group]
            if (
                any(alias in wanted for alias in aliases)
                and any(alias in actual for alias in aliases)
            ):
                return True
    return False


def evaluate_case(
    case: Dict[str, Any],
    response: Dict[str, Any],
) -> Dict[str, Any]:
    """评估单个 case 的 API/SSE 响应。"""
    request = TripRequest.model_validate(case["request"])
    expect = case.get("expect") or {}
    checks: List[Dict[str, Any]] = []

    success = bool(response.get("success"))
    degraded = bool(response.get("degraded"))
    checks.append(
        _check("response_success", success, "响应 success=true", weight=10)
    )
    checks.append(
        _check("not_degraded", not degraded, "响应不是降级占位行程", weight=15)
    )

    plan: Optional[TripPlan] = None
    try:
        plan = TripPlan.model_validate(response.get("data"))
        schema_ok = True
        schema_detail = "TripPlan schema 校验通过"
    except Exception as exc:
        schema_ok = False
        schema_detail = f"TripPlan schema 校验失败: {exc}"
    checks.append(_check("plan_schema", schema_ok, schema_detail, weight=20))

    if plan is not None:
        checks.append(
            _check(
                "city",
                plan.city.strip() == request.city.strip(),
                f"计划城市={plan.city!r}, 请求城市={request.city!r}",
                weight=5,
            )
        )
        checks.append(
            _check(
                "date_range",
                plan.start_date == request.start_date.isoformat()
                and plan.end_date == request.end_date.isoformat(),
                (
                    f"计划日期={plan.start_date}~{plan.end_date}, "
                    f"请求日期={request.start_date}~{request.end_date}"
                ),
                weight=10,
            )
        )
        checks.append(
            _check(
                "day_count",
                len(plan.days) == request.travel_days,
                f"计划天数={len(plan.days)}, 请求天数={request.travel_days}",
                weight=10,
            )
        )

        expected_dates = [
            (request.start_date + timedelta(days=index)).isoformat()
            for index in range(request.travel_days)
        ]
        actual_dates = [day.date for day in plan.days]
        actual_indexes = [day.day_index for day in plan.days]
        checks.append(
            _check(
                "day_sequence",
                actual_dates == expected_dates
                and actual_indexes == list(range(len(plan.days))),
                f"日期序列={actual_dates}, day_index={actual_indexes}",
                weight=10,
            )
        )

        min_per_day = max(1, int(expect.get("min_attractions_per_day", 1)))
        thin_days = [
            day.day_index + 1
            for day in plan.days
            if len(day.attractions) < min_per_day
        ]
        checks.append(
            _check(
                "attractions_per_day",
                not thin_days,
                f"景点不足的日期: {thin_days or '无'}",
                weight=10,
            )
        )

        attractions = _collect_attractions(plan)
        names = [item.name.strip() for item in attractions if item.name.strip()]
        unique_names = {
            _normalize_text(name) for name in names
        }
        min_unique = max(1, int(expect.get("min_unique_attractions", 1)))
        checks.append(
            _check(
                "unique_attractions",
                len(unique_names) >= min_unique,
                f"唯一景点={len(unique_names)}, 要求至少={min_unique}",
                weight=8,
            )
        )

        must_visit = _must_visit_names(request, expect)
        if must_visit:
            missing = [
                expected
                for expected in must_visit
                if not any(
                    _matches_must_visit(name, [expected])
                    for name in names
                )
            ]
            checks.append(
                _check(
                    "must_visit",
                    not missing,
                    f"未安排的点名地点: {missing or '无'}",
                    weight=15,
                )
            )

        bad_coordinates = [
            item.name
            for item in attractions
            if item.location is None
            or item.location.longitude == 0
            or item.location.latitude == 0
            or not -180 <= item.location.longitude <= 180
            or not -90 <= item.location.latitude <= 90
        ]
        checks.append(
            _check(
                "coordinates",
                not bad_coordinates,
                f"坐标异常景点: {bad_coordinates or '无'}",
                weight=6,
            )
        )

        route_quality = expect.get("route_quality")
        if isinstance(route_quality, dict):
            center = route_quality.get("center")
            max_distance = route_quality.get("max_center_distance_km")
            if center and isinstance(max_distance, (int, float)):
                max_remote_per_day = max(
                    0,
                    int(route_quality.get("max_remote_attractions_per_day", 0)),
                )
                remote_by_day: List[List[str]] = []
                remote_details: List[str] = []
                for day in plan.days:
                    remote = []
                    for attraction in day.attractions:
                        if _matches_must_visit(attraction.name, must_visit):
                            continue
                        distance = haversine_km(
                            center,
                            attraction.location,
                        )
                        if distance is not None and distance > float(max_distance):
                            remote.append(attraction.name)
                            remote_details.append(
                                f"第{day.day_index + 1}天 {attraction.name}"
                                f"({distance:.1f}km)"
                            )
                    remote_by_day.append(remote)

                worst_remote = max(
                    (len(items) for items in remote_by_day),
                    default=0,
                )
                checks.append(
                    _check(
                        "attraction_distance",
                        worst_remote <= max_remote_per_day,
                        (
                            f"超过 {float(max_distance):g}km 的非必去景点: "
                            f"{remote_details or '无'}; 单日上限={max_remote_per_day}"
                        ),
                        weight=10,
                    )
                )

            max_daily_route = route_quality.get("max_daily_route_km")
            if isinstance(max_daily_route, (int, float)):
                over_limit: List[str] = []
                measured: List[str] = []
                for day in plan.days:
                    has_must_visit = any(
                        _matches_must_visit(attraction.name, must_visit)
                        for attraction in day.attractions
                    )
                    if has_must_visit:
                        measured.append(f"第{day.day_index + 1}天=豁免必去")
                        continue
                    span = daily_route_span_km(day.attractions)
                    if span is None:
                        continue
                    measured.append(f"第{day.day_index + 1}天={span:.1f}km")
                    if span > float(max_daily_route):
                        over_limit.append(
                            f"第{day.day_index + 1}天={span:.1f}km"
                        )
                checks.append(
                    _check(
                        "daily_route",
                        not over_limit,
                        (
                            f"每日路线跨度: {', '.join(measured) or '无'}; "
                            f"单日上限={float(max_daily_route):g}km"
                        ),
                        weight=10,
                    )
                )

        placeholder = (
            "占位" in plan.overall_suggestions
            or any(
                re.search(r"景点\d+$", name)
                for name in names
            )
        )
        checks.append(
            _check(
                "no_placeholder",
                not placeholder,
                "未发现占位景点或占位提示",
                weight=10,
            )
        )
        checks.append(
            _check(
                "overview",
                bool(plan.overall_suggestions.strip()),
                "总体建议非空",
                weight=3,
            )
        )

        require_budget = bool(expect.get("require_budget", True))
        if require_budget or plan.budget is not None:
            budget_ok = plan.budget is not None
            budget_detail = "预算缺失"
            if plan.budget is not None:
                components = (
                    plan.budget.total_attractions
                    + plan.budget.total_hotels
                    + plan.budget.total_meals
                    + plan.budget.total_transportation
                )
                budget_ok = components == plan.budget.total
                budget_detail = (
                    f"预算分项合计={components}, total={plan.budget.total}"
                )
            checks.append(
                _check("budget", budget_ok, budget_detail, weight=6)
            )

    elapsed = response.get("elapsed")
    max_latency = float(
        expect.get("max_latency_seconds", DEFAULT_MAX_LATENCY_SECONDS)
    )
    latency_ok = isinstance(elapsed, (int, float)) and float(elapsed) <= max_latency
    checks.append(
        _check(
            "latency",
            latency_ok,
            f"耗时={elapsed!r}s, 上限={max_latency:g}s",
            weight=5,
        )
    )

    total_weight = sum(item["weight"] for item in checks)
    earned = sum(item["weight"] for item in checks if item["passed"])
    score = round(100 * earned / total_weight, 1) if total_weight else 0.0
    failed = [item["name"] for item in checks if not item["passed"]]
    return {
        "case_id": case["id"],
        "category": case.get("category", ""),
        "passed": not failed,
        "score": score,
        "degraded": degraded,
        "elapsed": float(elapsed) if isinstance(elapsed, (int, float)) else None,
        "failed_checks": failed,
        "checks": checks,
        "message": response.get("message", ""),
        "reason": response.get("reason", ""),
    }


def build_report(
    results: Iterable[Dict[str, Any]],
    *,
    mode: str,
    backend: Optional[str] = None,
) -> Dict[str, Any]:
    """汇总单个 case 结果。"""
    items = list(results)
    total = len(items)
    passed = sum(1 for item in items if item["passed"])
    latencies = [
        item["elapsed"] for item in items if item["elapsed"] is not None
    ]
    average_latency = (
        round(sum(latencies) / len(latencies), 2) if latencies else None
    )
    return {
        "schema_version": 1,
        "mode": mode,
        "backend": backend,
        "case_count": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": round(100 * passed / total, 1) if total else 0.0,
        "average_score": (
            round(sum(item["score"] for item in items) / total, 1)
            if total
            else 0.0
        ),
        "average_latency_seconds": average_latency,
        "degraded_count": sum(1 for item in items if item["degraded"]),
        "results": items,
    }


def render_markdown(report: Dict[str, Any]) -> str:
    """把报告渲染成便于查看的 Markdown。"""
    lines = [
        "# 旅行规划质量评测",
        "",
        f"- 模式：`{report['mode']}`",
        f"- 后端：`{report.get('backend') or 'replay'}`",
        f"- Case 数：{report['case_count']}",
        f"- 通过：{report['passed']} / {report['case_count']}",
        f"- 通过率：{report['pass_rate']}%",
        f"- 平均分：{report['average_score']}",
        f"- 平均耗时：{report['average_latency_seconds'] or '-'} 秒",
        f"- 降级结果：{report['degraded_count']}",
        "",
        "| 状态 | Case | 分类 | 分数 | 耗时 | 失败检查 |",
        "| --- | --- | --- | ---: | ---: | --- |",
    ]
    for item in report["results"]:
        status = "PASS" if item["passed"] else "FAIL"
        elapsed = f"{item['elapsed']:.1f}s" if item["elapsed"] is not None else "-"
        failed = ", ".join(item["failed_checks"]) or "-"
        lines.append(
            f"| {status} | `{item['case_id']}` | {item['category']} | "
            f"{item['score']} | {elapsed} | {failed} |"
        )
    return "\n".join(lines) + "\n"
