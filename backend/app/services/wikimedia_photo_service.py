"""从 Wikimedia Commons 查询景点图片,并提供快速失败与缓存。"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode, urlparse

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
_FAILURE_BACKOFF_SECONDS = 20
_FAILURE_THRESHOLD = 2
_TARGET_IMAGE_WIDTH = 1600
_MIN_IMAGE_WIDTH = 800
_MIN_IMAGE_HEIGHT = 500
_MISSING = object()
_AUTO_CURL = object()
_NON_PHOTO_TITLE_PATTERN = re.compile(
    r"(图卷|画卷|绘画|畫作|书法|書法|地图|地圖|平面图|平面圖|"
    r"poster|painting|drawing|diagram|illustration|manuscript|"
    r"calligraphy|map\b|logo|scan\b|book\s+cover)",
    re.IGNORECASE,
)


class PhotoLookupError(RuntimeError):
    """图片搜索的网络或响应错误。"""


@dataclass(frozen=True)
class AttractionPhoto:
    """一张通过服务端校验的景点图片。"""

    url: str
    provider: str
    source_url: str
    title: str
    scope: str
    width: int = 0
    height: int = 0


class WikimediaPhotoService:
    """用 Commons API 搜索文件名,只返回可安全嵌入的位图。"""

    def __init__(
        self,
        *,
        timeout: float = 10.0,
        curl_path: Optional[str] | object = _AUTO_CURL,
    ):
        self.timeout = timeout
        self.base_url = "https://commons.wikimedia.org/w/api.php"
        self.curl_path = (
            shutil.which("curl")
            if curl_path is _AUTO_CURL
            else curl_path
        )
        self._lock = threading.Lock()
        self._cache: Dict[str, Tuple[float, Optional[AttractionPhoto]]] = {}
        self._unavailable_until = 0.0
        self._consecutive_failures = 0

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
            photo = self._search(
                " ".join(part for part in (clean_name, clean_city) if part)
            )
        except (
            PhotoLookupError,
            requests.RequestException,
            TypeError,
            ValueError,
        ) as exc:
            print(f"⚠️  Wikimedia 图片搜索失败: {type(exc).__name__}: {exc}")
            with self._lock:
                self._consecutive_failures += 1
                if self._consecutive_failures >= _FAILURE_THRESHOLD:
                    self._unavailable_until = (
                        time.monotonic() + _FAILURE_BACKOFF_SECONDS
                    )
            self._set_cache(cache_key, None, ttl=_FAILURE_TTL_SECONDS)
            return None

        with self._lock:
            self._consecutive_failures = 0
            self._unavailable_until = 0.0

        self._set_cache(
            cache_key,
            photo,
            ttl=_SUCCESS_TTL_SECONDS if photo else _NO_MATCH_TTL_SECONDS,
        )
        return photo

    def _search(self, query: str) -> Optional[AttractionPhoto]:
        payload = self._fetch_json(
            {
                "action": "query",
                "generator": "search",
                "gsrsearch": query,
                "gsrnamespace": 6,
                "gsrlimit": 10,
                "prop": "imageinfo",
                "iiprop": "url|mime|size|dimensions",
                "iiurlwidth": _TARGET_IMAGE_WIDTH,
                "format": "json",
                "origin": "*",
            }
        )
        pages = (payload.get("query") or {}).get("pages") or {}
        if not isinstance(pages, dict):
            return None

        ordered: List[Dict[str, Any]] = sorted(
            (page for page in pages.values() if isinstance(page, dict)),
            key=self._page_index,
        )
        candidates: List[Tuple[float, AttractionPhoto]] = []
        query_terms = [
            term.casefold()
            for term in query.split()
            if len(term.strip()) >= 2
        ]
        for page in ordered:
            image_info = page.get("imageinfo") or []
            if not image_info or not isinstance(image_info[0], dict):
                continue
            info = image_info[0]
            if str(info.get("mime") or "").lower() not in _ALLOWED_MIME_TYPES:
                continue
            title = str(page.get("title") or "").removeprefix("File:")
            if _NON_PHOTO_TITLE_PATTERN.search(title):
                continue
            width = self._positive_int(info.get("width"))
            height = self._positive_int(info.get("height"))
            if width and width < _MIN_IMAGE_WIDTH:
                continue
            if height and height < _MIN_IMAGE_HEIGHT:
                continue
            if width and height and max(width, height) / min(width, height) > 3.2:
                continue
            image_url = str(info.get("thumburl") or info.get("url") or "").strip()
            if not self._is_safe_image_url(image_url):
                continue
            source_url = str(info.get("descriptionurl") or "").strip()
            if source_url and not source_url.startswith(
                "https://commons.wikimedia.org/"
            ):
                source_url = ""
            candidate = AttractionPhoto(
                url=image_url,
                provider="Wikimedia Commons",
                source_url=source_url,
                title=title,
                scope="attraction",
                width=width,
                height=height,
            )
            score = self._candidate_score(
                candidate,
                page_index=self._page_index(page),
                query_terms=query_terms,
            )
            candidates.append((score, candidate))
        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]

    @staticmethod
    def _candidate_score(
        photo: AttractionPhoto,
        *,
        page_index: int,
        query_terms: List[str],
    ) -> float:
        """优先高清、横向实景图，同时保留搜索相关性。"""
        title = photo.title.casefold()
        score = max(0.0, 30.0 - page_index * 3.0)
        score += sum(18.0 for term in query_terms if term in title)
        if photo.width >= 1800:
            score += 20.0
        elif photo.width >= 1200:
            score += 12.0
        if photo.height >= 900:
            score += 8.0
        if photo.width and photo.height:
            ratio = photo.width / photo.height
            if 1.15 <= ratio <= 2.2:
                score += 12.0
        return score

    def _fetch_json(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """优先用系统 curl(Windows Schannel),其它平台回退 requests。"""
        if self.curl_path:
            url = f"{self.base_url}?{urlencode(params)}"
            try:
                result = subprocess.run(
                    [
                        str(self.curl_path),
                        "--silent",
                        "--show-error",
                        "--location",
                        "--connect-timeout",
                        "8",
                        "--max-time",
                        "20",
                        "--retry",
                        "2",
                        "--retry-delay",
                        "1",
                        "--retry-all-errors",
                        "--user-agent",
                        (
                            "helloagents-trip-planner/1.0 "
                            "(local development; attraction photo lookup)"
                        ),
                        url,
                    ],
                    capture_output=True,
                    check=False,
                    timeout=25,
                )
            except (subprocess.TimeoutExpired, OSError) as exc:
                raise PhotoLookupError(f"curl 请求失败: {exc}") from exc
            if result.returncode != 0:
                message = result.stderr.decode("utf-8", errors="replace").strip()
                raise PhotoLookupError(message or f"curl 退出码 {result.returncode}")
            try:
                payload = json.loads(result.stdout.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise PhotoLookupError("Wikimedia 返回了非法 JSON") from exc
            return payload if isinstance(payload, dict) else {}

        response = requests.get(
            self.base_url,
            params=params,
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
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _page_index(page: Dict[str, Any]) -> int:
        try:
            return int(page.get("index") or 9999)
        except (TypeError, ValueError):
            return 9999

    @staticmethod
    def _positive_int(value: Any) -> int:
        try:
            parsed = int(value or 0)
        except (TypeError, ValueError):
            return 0
        return max(parsed, 0)

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
