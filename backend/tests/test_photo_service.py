"""景点图片查询服务与接口测试。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.api.main import app
from app.api.routes import poi as poi_routes
from app.services.wikimedia_photo_service import (
    AttractionPhoto,
    WikimediaPhotoService,
)


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_wikimedia_service_rejects_documents_and_uses_bitmap(monkeypatch):
    service = WikimediaPhotoService()
    payload = {
        "query": {
            "pages": {
                "1": {
                    "index": 1,
                    "title": "File:guide.pdf",
                    "imageinfo": [
                        {
                            "mime": "application/pdf",
                            "thumburl": "https://upload.wikimedia.org/guide.jpg",
                        }
                    ],
                },
                "2": {
                    "index": 2,
                    "title": "File:Beihu Park overview.JPG",
                    "imageinfo": [
                        {
                            "mime": "image/jpeg",
                            "thumburl": (
                                "https://upload.wikimedia.org/"
                                "wikipedia/commons/beihu.jpg"
                            ),
                            "descriptionurl": (
                                "https://commons.wikimedia.org/wiki/"
                                "File:Beihu_Park_overview.JPG"
                            ),
                        }
                    ],
                },
            }
        }
    }
    monkeypatch.setattr(
        "app.services.wikimedia_photo_service.requests.get",
        lambda *args, **kwargs: _Response(payload),
    )

    photo = service.get_photo("北湖公园", "郴州")

    assert photo is not None
    assert photo.provider == "Wikimedia Commons"
    assert photo.title == "Beihu Park overview.JPG"
    assert photo.url.startswith("https://upload.wikimedia.org/")


def test_wikimedia_service_caches_no_match(monkeypatch):
    service = WikimediaPhotoService()
    calls = []

    def _get(*args, **kwargs):
        calls.append(args)
        return _Response({"query": {"pages": {}}})

    monkeypatch.setattr(
        "app.services.wikimedia_photo_service.requests.get",
        _get,
    )

    assert service.get_photo("不存在的地方", "郴州") is None
    assert service.get_photo("不存在的地方", "郴州") is None
    assert len(calls) == 1


def test_photo_route_returns_metadata(monkeypatch):
    class _PhotoService:
        def get_photo(self, name, city):
            return AttractionPhoto(
                url="https://upload.wikimedia.org/test.jpg",
                provider="Wikimedia Commons",
                source_url="https://commons.wikimedia.org/wiki/File:Test.jpg",
                title="Test.jpg",
            )

    monkeypatch.setattr(
        poi_routes,
        "get_wikimedia_photo_service",
        lambda: _PhotoService(),
    )
    client = TestClient(app)

    response = client.get(
        "/api/poi/photo",
        params={"name": "北湖公园", "city": "郴州"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["data"]["photo_url"] == "https://upload.wikimedia.org/test.jpg"
    assert body["data"]["provider"] == "Wikimedia Commons"
