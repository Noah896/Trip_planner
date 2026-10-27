"""从 Wikimedia Commons 查询景点图片,并提供快速失败与缓存。"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests


_ALLOWED_MIME_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
}
_SUCCESS_TTL_SECONDS = 24 * 60 * 60
_NO_MATCH_TTL_SECONDS = 10 * 60
_FAILURE_TTL_SECONDS = 60
_FAILURE_BACKOFF_SECONDS = 60
_MISSING = object()


@dataclass(frozen=True)
class AttractionPhoto:
    """一张通过服务端校验的景点图片。"""

    url: str
    provider: str
    source_url: str
    title: str


class WikimediaPhotoService:
    """用 Commons API 搜索文件名,只返回可安全嵌入的位图。"""

    def __init__(
        self,
        *,
        timeout: Tuple[float, float] = (3.0, 8.0),
    ):
        self.timeout = timeout
        self.base_url = "https://commons.wikimedia.org/w/api.php"
        self._lock = threading.Lock()
        self._cache: Dict[str, Tuple[float, Optional[AttractionPhoto]]] = {}
        self._unavailable_until = 0.0

    def get_photo(
        self,
        name: str,
        city: str = "",
    ) -> Optional[AttractionPhoto]:
        """查询景点图片;网络失败时短暂熔断,避免页面并发打穿上游。"""
        clean_name = " ".join((name or "").split()).strip()
        if not clean_name:
            return None
        clean_city = " ".join((city or "").split()).strip()
        cache_key = f"{clean_name}|{clean_city}".casefold()

        cached = self._get_cache(cache_key)
        if cached is not _MISSING:
            return cached

        with self._lock:
            if time.monotonic() < self._unavailable_until:
                return None

        try:
            photo = self._search(clean_name, clean_city)
        except (requests.RequestException, TypeError, ValueError) as exc:
            print(f"⚠️  Wikimedia 图片搜索失败: {type(exc).__name__}: {exc}")
            with self._lock:
                self._unavailable_until = time.monotonic() + _FAILURE_BACKOFF_SECONDS
            self._set_cache(cache_key, None, ttl=_FAILURE_TTL_SECONDS)
            return None

        self._set_cache(
            cache_key,
            photo,
            ttl=_SUCCESS_TTL_SECONDS if photo else _NO_MATCH_TTL_SECONDS,
        )
        return photo

    def _search(
        self,
        name: str,
        city: str,
    ) -> Optional[AttractionPhoto]:
        query = " ".join(part for part in (name, city) if part)
        response = requests.get(
            self.base_url,
            params={
                "action": "query",
                "generator": "search",
                "gsrsearch": query,
                "gsrnamespace": 6,
                "gsrlimit": 5,
                "prop": "imageinfo",
                "iiprop": "url|mime",
                "iiurlwidth": 800,
                "format": "json",
                "origin": "*",
            },
            headers={
                "User-Agent": (
                    "helloagents-trip-planner/1.0 "
                    "(local development; attraction photo lookup)"
                )
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        pages = (payload.get("query") or {}).get("pages") or {}
        if not isinstance(pages, dict):
            return None

        ordered: List[Dict[str, Any]] = sorted(
            (page for page in pages.values() if isinstance(page, dict)),
            key=self._page_index,
        )
        for page in ordered:
            image_info = page.get("imageinfo") or []
            if not image_info or not isinstance(image_info[0], dict):
                continue
            info = image_info[0]
            if str(info.get("mime") or "").lower() not in _ALLOWED_MIME_TYPES:
                continue
            image_url = str(info.get("thumburl") or info.get("url") or "").strip()
            if not self._is_safe_image_url(image_url):
                continue
            source_url = str(info.get("descriptionurl") or "").strip()
            if source_url and not source_url.startswith(
                "https://commons.wikimedia.org/"
            ):
                source_url = ""
            return AttractionPhoto(
                url=image_url,
                provider="Wikimedia Commons",
                source_url=source_url,
                title=str(page.get("title") or "").removeprefix("File:"),
            )
        return None

    @staticmethod
    def _page_index(page: Dict[str, Any]) -> int:
        try:
            return int(page.get("index") or 9999)
        except (TypeError, ValueError):
            return 9999

    @staticmethod
    def _is_safe_image_url(value: str) -> bool:
        if not value.startswith("https://"):
            return False
        host = (urlparse(value).hostname or "").lower()
        return host in {"upload.wikimedia.org", "thumb.wikimedia.org"}

    def _get_cache(
        self,
        key: str,
    ) -> Optional[AttractionPhoto] | object:
        now = time.monotonic()
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return _MISSING
            expires_at, value = entry
            if expires_at <= now:
                self._cache.pop(key, None)
                return _MISSING
            return value

    def _set_cache(
        self,
        key: str,
        value: Optional[AttractionPhoto],
        *,
        ttl: int,
    ) -> None:
        with self._lock:
            self._cache[key] = (time.monotonic() + ttl, value)

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()
            self._unavailable_until = 0.0


_photo_service: Optional[WikimediaPhotoService] = None


def get_wikimedia_photo_service() -> WikimediaPhotoService:
    global _photo_service
    if _photo_service is None:
        _photo_service = WikimediaPhotoService()
    return _photo_service
