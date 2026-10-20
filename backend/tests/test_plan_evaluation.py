"""旅行规划评测器离线测试。"""

import copy
import json
from pathlib import Path

from evals.evaluator import (
    build_report,
    evaluate_case,
    load_golden_cases,
    render_markdown,
)


ROOT = Path(__file__).resolve().parent.parent


def _cases():
    return load_golden_cases()


def _sample_responses():
    payload = json.loads(
        (ROOT / "evals" / "fixtures" / "sample_replay.json").read_text(
            encoding="utf-8"
        )
    )
    return payload["responses"]


def test_golden_cases_are_valid_and_cover_required_scenarios():
    cases = _cases()

    assert len(cases) == 19
    assert len({case["id"] for case in cases}) == len(cases)
    assert {"classic", "family", "food", "must_visit", "nature"} <= {
        case["category"] for case in cases
    }
    assert any(case["request"]["must_visit"] for case in cases)
    assert any(case["request"]["travel_days"] == 2 for case in cases)
    assert any(case["request"]["travel_days"] == 4 for case in cases)


def test_valid_sample_responses_pass():
    cases = {case["id"]: case for case in _cases()}
    responses = _sample_responses()

    for case_id in ("beijing-classic-3d", "beijing-must-visit"):
        result = evaluate_case(cases[case_id], responses[case_id])
        assert result["passed"] is True
        assert result["score"] == 100.0
        assert result["failed_checks"] == []


def test_degraded_placeholder_fails_quality_checks():
    cases = {case["id"]: case for case in _cases()}
    result = evaluate_case(
        cases["shanghai-family-3d"],
        _sample_responses()["shanghai-family-3d"],
    )

    assert result["passed"] is False
    assert result["degraded"] is True
    assert "not_degraded" in result["failed_checks"]
    assert "no_placeholder" in result["failed_checks"]


def test_missing_must_visit_and_date_mismatch_are_detected():
    cases = {case["id"]: case for case in _cases()}
    response = copy.deepcopy(_sample_responses()["beijing-must-visit"])
    response["data"]["days"][0]["attractions"][0]["name"] = "北海公园"
    response["data"]["days"][1]["date"] = "2026-11-10"

    result = evaluate_case(cases["beijing-must-visit"], response)

    assert "must_visit" in result["failed_checks"]
    assert "day_sequence" in result["failed_checks"]


def test_terracotta_museum_name_is_accepted_as_bingmayong_alias():
    cases = {case["id"]: case for case in _cases()}
    response = copy.deepcopy(_sample_responses()["beijing-must-visit"])
    response["data"]["city"] = "西安"
    response["data"]["start_date"] = "2026-11-27"
    response["data"]["end_date"] = "2026-11-29"
    response["data"]["days"][0]["date"] = "2026-11-27"
    response["data"]["days"][1]["date"] = "2026-11-28"
    response["data"]["days"][2]["date"] = "2026-11-29"
    response["data"]["days"][0]["attractions"][0]["name"] = "秦始皇帝陵博物院"

    result = evaluate_case(cases["xian-terracotta-3d"], response)

    assert result["passed"] is True
    assert "must_visit" not in result["failed_checks"]


def _beijing_family_response():
    response = copy.deepcopy(_sample_responses()["beijing-classic-3d"])
    response["data"]["start_date"] = "2026-11-21"
    response["data"]["end_date"] = "2026-11-23"
    response["data"]["days"][0]["date"] = "2026-11-21"
    response["data"]["days"][1]["date"] = "2026-11-22"
    response["data"]["days"][2]["date"] = "2026-11-23"
    return response


def test_family_route_quality_passes_for_compact_plan():
    cases = {case["id"]: case for case in _cases()}

    result = evaluate_case(
        cases["beijing-family-3d"],
        _beijing_family_response(),
    )

    assert result["passed"] is True
    assert "attraction_distance" not in result["failed_checks"]
    assert "daily_route" not in result["failed_checks"]


def test_family_route_quality_rejects_remote_attraction():
    cases = {case["id"]: case for case in _cases()}
    response = _beijing_family_response()
    response["data"]["days"][0]["attractions"][0].update(
        {
            "name": "金海湖",
            "address": "平谷区金海湖镇",
            "location": {"longitude": 117.24, "latitude": 40.17},
        }
    )

    result = evaluate_case(cases["beijing-family-3d"], response)

    assert result["passed"] is False
    assert "attraction_distance" in result["failed_checks"]


def test_remote_must_visit_is_exempt_from_route_quality():
    case = copy.deepcopy(
        {item["id"]: item for item in _cases()}["beijing-family-3d"]
    )
    case["request"]["must_visit"] = ["金海湖"]
    case["expect"]["must_visit"] = ["金海湖"]
    response = _beijing_family_response()
    response["data"]["days"][0]["attractions"][0].update(
        {
            "name": "金海湖",
            "address": "平谷区金海湖镇",
            "location": {"longitude": 117.24, "latitude": 40.17},
        }
    )

    result = evaluate_case(case, response)

    assert result["passed"] is True
    assert "attraction_distance" not in result["failed_checks"]


def test_report_and_markdown_include_failure_details():
    cases = {case["id"]: case for case in _cases()}
    responses = _sample_responses()
    results = [
        evaluate_case(cases["beijing-classic-3d"], responses["beijing-classic-3d"]),
        evaluate_case(cases["shanghai-family-3d"], responses["shanghai-family-3d"]),
    ]

    report = build_report(results, mode="replay")
    markdown = render_markdown(report)

    assert report["case_count"] == 2
    assert report["passed"] == 1
    assert report["failed"] == 1
    assert report["pass_rate"] == 50.0
    assert "shanghai-family-3d" in markdown
    assert "not_degraded" in markdown
