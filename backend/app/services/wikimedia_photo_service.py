"""从 Wikimedia Commons 查询景点图片,并提供快速失败与缓存。"""

from __future__ import annotations

import json
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
_FAILURE_BACKOFF_SECONDS = 60
_MISSING = object()
_AUTO_CURL = object()


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
                self._unavailable_until = time.monotonic() + _FAILURE_BACKOFF_SECONDS
            self._set_cache(cache_key, None, ttl=_FAILURE_TTL_SECONDS)
            return None

        if photo is None and clean_city:
            city_key = f"__city__|{clean_city}".casefold()
            city_photo = self._get_cache(city_key)
            if city_photo is _MISSING:
                try:
                    city_photo = self._search(
                        f"{clean_city} 风景",
                        variant_seed=clean_name,
                    )
                    if city_photo is not None:
                        city_photo = AttractionPhoto(
                            url=city_photo.url,
                            provider=city_photo.provider,
                            source_url=city_photo.source_url,
                            title=city_photo.title,
                            scope="city",
                        )
                except (
                    PhotoLookupError,
                    requests.RequestException,
                    TypeError,
                    ValueError,
                ) as exc:
                    print(
                        "⚠️  Wikimedia 城市风光搜索失败: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    city_photo = None
                self._set_cache(
                    city_key,
                    city_photo,
                    ttl=(
                        _SUCCESS_TTL_SECONDS
                        if city_photo
                        else _NO_MATCH_TTL_SECONDS
                    ),
                )
            if city_photo is not _MISSING:
                photo = city_photo

        self._set_cache(
            cache_key,
            photo,
            ttl=_SUCCESS_TTL_SECONDS if photo else _NO_MATCH_TTL_SECONDS,
        )
        return photo

    def _search(
        self,
        query: str,
        *,
        variant_seed: str = "",
    ) -> Optional[AttractionPhoto]:
        payload = self._fetch_json(
            {
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
            }
        )
        pages = (payload.get("query") or {}).get("pages") or {}
        if not isinstance(pages, dict):
            return None

        ordered: List[Dict[str, Any]] = sorted(
            (page for page in pages.values() if isinstance(page, dict)),
            key=self._page_index,
        )
        candidates: List[AttractionPhoto] = []
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
            candidates.append(
                AttractionPhoto(
                    url=image_url,
                    provider="Wikimedia Commons",
                    source_url=source_url,
                    title=str(page.get("title") or "").removeprefix("File:"),
                    scope="attraction",
                )
            )
        if not candidates:
            return None
        if not variant_seed:
            return candidates[0]
        index = sum(ord(char) for char in variant_seed) % len(candidates)
        return candidates[index]

    def _fetch_json(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """优先用系统 curl(Windows Schannel),其它平台回退 requests。"""
        if self.curl_path:
            url = f"{self.base_url}?{urlencode(params)}"
            result = subprocess.run(
                [
                    str(self.curl_path),
                    "--silent",
                    "--show-error",
                    "--location",
                    "--connect-timeout",
                    "5",
                    "--max-time",
                    "12",
                    "--retry",
                    "1",
                    "--user-agent",
                    (
                        "helloagents-trip-planner/1.0 "
                        "(local development; attraction photo lookup)"
                    ),
                    url,
                ],
                capture_output=True,
                check=False,
                timeout=15,
            )
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
