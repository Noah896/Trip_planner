"""景点官网解析与跳转接口测试。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.api.main import app
from app.api.routes import poi as poi_routes
from app.services.attraction_info_service import (
    AttractionInfoLink,
    AttractionInfoService,
    _SearchResult,
)


def test_resolver_prefers_repeated_official_domain(monkeypatch):
    service = AttractionInfoService()
    candidates = [
        _SearchResult("故宫博物院", "https://www.so.com/link?m=one"),
        _SearchResult("导览 - 故宫博物院", "https://www.so.com/link?m=two"),
    ]
    targets = {
        "https://www.so.com/link?m=one": "https://www.dpm.org.cn/visit.html",
        "https://www.so.com/link?m=two": "https://www.dpm.org.cn/guides.html",
    }
    monkeypatch.setattr(service, "_fetch_search_results", lambda *_: candidates)
    monkeypatch.setattr(
        service,
        "_resolve_result_url",
        lambda href: targets[href],
    )

    result = service.resolve("故宫博物院", "北京")

    assert result.source == "official"
    assert result.url == "https://www.dpm.org.cn/"


def test_resolver_rejects_aggregator_even_if_title_says_official(monkeypatch):
    service = AttractionInfoService()
    monkeypatch.setattr(
        service,
        "_fetch_search_results",
        lambda *_: [
            _SearchResult(
                "湖南省植物园官网线上预约入口",
                "https://www.so.com/link?m=aggregator",
            )
        ],
    )
    monkeypatch.setattr(
        service,
        "_resolve_result_url",
        lambda _: "https://cs.bendibao.com/tour/123.shtm",
    )

    result = service.resolve("湖南省植物园", "长沙")

    assert result.source == "search"
    assert result.url.startswith("https://www.so.com/s?q=")


def test_resolver_caches_official_result(monkeypatch):
    service = AttractionInfoService()
    calls = []

    def _fetch(name, city):
        calls.append((name, city))
        return [
            _SearchResult(f"{name}官网", "https://www.so.com/link?m=official"),
            _SearchResult(f"{name}介绍", "https://www.so.com/link?m=guide"),
        ]

    monkeypatch.setattr(service, "_fetch_search_results", _fetch)
    monkeypatch.setattr(
        service,
        "_resolve_result_url",
        lambda _: "http://www.hnfbg.cn/yndt",
    )

    first = service.resolve("湖南省植物园", "长沙")
    second = service.resolve("湖南省植物园", "长沙")

    assert first == second
    assert first.url == "http://www.hnfbg.cn/"
    assert calls == [("湖南省植物园", "长沙")]


def test_official_site_route_redirects(monkeypatch):
    class _InfoService:
        def resolve(self, name: str, city: str):
            assert name == "故宫博物院"
            assert city == "北京"
            return AttractionInfoLink(
                url="https://www.dpm.org.cn/",
                source="official",
            )

    monkeypatch.setattr(
        poi_routes,
        "get_attraction_info_service",
        lambda: _InfoService(),
    )

    response = TestClient(app).get(
        "/api/poi/official-site",
        params={"name": "故宫博物院", "city": "北京"},
        follow_redirects=False,
    )

    assert response.status_code == 307
    assert response.headers["location"] == "https://www.dpm.org.cn/"
    assert response.headers["x-attraction-info-source"] == "official"


def test_official_site_route_uses_safe_in_app_fallback(monkeypatch):
    class _InfoService:
        def resolve(self, name: str, city: str):
            return AttractionInfoLink(
                url="https://www.so.com/s?q=fallback",
                source="search",
            )

    monkeypatch.setattr(
        poi_routes,
        "get_attraction_info_service",
        lambda: _InfoService(),
    )
    fallback = "/attraction-info?name=%E4%BA%94%E5%B2%AD%E9%98%81&city=%E9%83%B4%E5%B7%9E"

    response = TestClient(app).get(
        "/api/poi/official-site",
        params={
            "name": "五岭阁",
            "city": "郴州",
            "fallback": fallback,
        },
        follow_redirects=False,
    )

    assert response.status_code == 307
    assert response.headers["location"] == fallback
    assert response.headers["x-attraction-info-source"] == "search"


def test_official_site_route_rejects_external_fallback(monkeypatch):
    class _InfoService:
        def resolve(self, name: str, city: str):
            return AttractionInfoLink(
                url="https://www.so.com/s?q=fallback",
                source="search",
            )

    monkeypatch.setattr(
        poi_routes,
        "get_attraction_info_service",
        lambda: _InfoService(),
    )

    response = TestClient(app).get(
        "/api/poi/official-site",
        params={
            "name": "五岭阁",
            "city": "郴州",
            "fallback": "https://example.com/redirect",
        },
        follow_redirects=False,
    )

    assert response.status_code == 307
    assert response.headers["location"] == "https://www.so.com/s?q=fallback"
