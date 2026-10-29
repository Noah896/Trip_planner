"""Resolve an attraction's official website with a search-page fallback."""

from __future__ import annotations

import html
import ipaddress
import re
import threading
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote_plus, urljoin, urlparse

import httpx


_SEARCH_BASE = "https://www.so.com/s"
_SUCCESS_TTL_SECONDS = 24 * 60 * 60
_FAILURE_TTL_SECONDS = 10 * 60
_MAX_CACHE_ENTRIES = 256
_OFFICIAL_HINTS = ("官网", "官方网站", "官方站点")
_BLOCKED_HOSTS = (
    "amap.com",
    "autonavi.com",
    "baidu.com",
    "bendibao.com",
    "bilibili.com",
    "city8.com",
    "ctrip.com",
    "dianping.com",
    "douyin.com",
    "elong.com",
    "fliggy.com",
    "jd.com",
    "ly.com",
    "mafengwo.cn",
    "map.360.cn",
    "meituan.com",
    "qunar.com",
    "so.com",
    "sohu.com",
    "trip.com",
    "tuniu.com",
    "taobao.com",
    "weibo.com",
    "xiaohongshu.com",
    "zhihu.com",
)
_REDIRECT_PATTERN = re.compile(
    r"""window\.location\.replace\(\s*["']([^"']+)["']\s*\)""",
    re.IGNORECASE,
)
_META_REDIRECT_PATTERN = re.compile(
    r"""<meta[^>]+http-equiv=["']?refresh["']?[^>]+content=["'][^"']*url=([^"'>]+)""",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class AttractionInfoLink:
    url: str
    source: str
    title: str = ""


@dataclass(frozen=True)
class _SearchResult:
    title: str
    href: str


class _SearchResultParser(HTMLParser):
    """Extract organic titles from the search result page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: List[_SearchResult] = []
        self._in_title = False
        self._href = ""
        self._text: List[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        if tag == "h3" and "res-title" in classes:
            self._in_title = True
            self._href = ""
            self._text = []
            return
        if self._in_title and tag == "a" and attributes.get("href"):
            self._href = attributes["href"]

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "h3" or not self._in_title:
            return
        title = " ".join("".join(self._text).split())
        if title and self._href:
            self.results.append(_SearchResult(title=title, href=self._href))
        self._in_title = False


def _compact(value: str) -> str:
    return re.sub(r"[\s_|·\-—]+", "", value or "").casefold()


def _host_matches(host: str, blocked: str) -> bool:
    return host == blocked or host.endswith(f".{blocked}")


def _is_safe_external_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    if parsed.username or parsed.password:
        return False
    host = parsed.hostname.strip().casefold()
    if not host or host in {"localhost", "localhost.localdomain"}:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "." in host
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_unspecified
    )


class AttractionInfoService:
    """Find official attraction sites without trusting arbitrary search hits."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: Dict[
            str, Tuple[float, AttractionInfoLink]
        ] = {}

    def resolve(self, name: str, city: str = "") -> AttractionInfoLink:
        clean_name = " ".join((name or "").split())
        clean_city = " ".join((city or "").split())
        if not clean_name:
            return self._fallback("", clean_city)

        cache_key = f"{clean_name.casefold()}|{clean_city.casefold()}"
        cached = self._get_cache(cache_key)
        if cached is not None:
            return cached

        try:
            result = self._lookup(clean_name, clean_city)
        except Exception as exc:  # noqa: BLE001 - external lookup must not break the page
            print(f"⚠️  景点官网解析失败,回退到介绍页: {type(exc).__name__}: {exc}")
            result = self._fallback(clean_name, clean_city)

        self._set_cache(
            cache_key,
            result,
            ttl=(
                _SUCCESS_TTL_SECONDS
                if result.source == "official"
                else _FAILURE_TTL_SECONDS
            ),
        )
        return result

    def _lookup(self, name: str, city: str) -> AttractionInfoLink:
        candidates = self._fetch_search_results(name, city)
        compact_name = _compact(name)
        host_counts: Dict[str, int] = {}
        for candidate in candidates:
            compact_title = _compact(candidate.title)
            if compact_name not in compact_title:
                continue
            target = self._resolve_result_url(candidate.href)
            if not target or not self._is_trusted_target(target):
                continue
            parsed = urlparse(target)
            target = f"{parsed.scheme}://{parsed.netloc}/"
            host = (parsed.hostname or "").casefold()
            host_counts[host] = host_counts.get(host, 0) + 1
            has_official_hint = any(
                hint in compact_title for hint in _OFFICIAL_HINTS
            )
            # 同一官方域名出现在多个景点相关结果中，比单条“官网”标题可靠。
            if has_official_hint or host_counts[host] >= 2:
                print(f"🔗 景点官网解析: {name} -> {target}")
                return AttractionInfoLink(
                    url=target,
                    source="official",
                    title=candidate.title,
                )
        return self._fallback(name, city)

    def _fetch_search_results(self, name: str, city: str) -> List[_SearchResult]:
        query = " ".join(part for part in (name, city, "官网") if part)
        with httpx.Client(
            follow_redirects=True,
            timeout=8.0,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/124 Safari/537.36"
                )
            },
        ) as client:
            response = client.get(_SEARCH_BASE, params={"q": query})
            response.raise_for_status()
            parser = _SearchResultParser()
            parser.feed(response.text)
        return parser.results[:12]

    def _resolve_result_url(self, href: str) -> str:
        target = urljoin(_SEARCH_BASE, html.unescape(href))
        parsed = urlparse(target)
        if not parsed.hostname or not _host_matches(
            parsed.hostname.casefold(), "so.com"
        ):
            return target

        with httpx.Client(
            follow_redirects=True,
            timeout=8.0,
            headers={"User-Agent": "Mozilla/5.0"},
        ) as client:
            response = client.get(target)
            response.raise_for_status()
        match = _REDIRECT_PATTERN.search(response.text)
        if match:
            return html.unescape(match.group(1))
        match = _META_REDIRECT_PATTERN.search(response.text)
        return html.unescape(match.group(1)) if match else ""

    @staticmethod
    def _is_trusted_target(url: str) -> bool:
        if not _is_safe_external_url(url):
            return False
        host = (urlparse(url).hostname or "").casefold()
        return not any(_host_matches(host, blocked) for blocked in _BLOCKED_HOSTS)

    @staticmethod
    def _fallback(name: str, city: str) -> AttractionInfoLink:
        query = " ".join(part for part in (name, city, "官网", "介绍") if part)
        return AttractionInfoLink(
            url=f"{_SEARCH_BASE}?q={quote_plus(query)}",
            source="search",
            title=f"{name}官网与介绍",
        )

    def _get_cache(self, key: str) -> Optional[AttractionInfoLink]:
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(key)
            if cached is None:
                return None
            expires_at, value = cached
            if expires_at <= now:
                self._cache.pop(key, None)
                return None
            return value

    def _set_cache(self, key: str, value: AttractionInfoLink, *, ttl: int) -> None:
        with self._lock:
            if len(self._cache) >= _MAX_CACHE_ENTRIES:
                oldest = min(self._cache, key=lambda item: self._cache[item][0])
                self._cache.pop(oldest, None)
            self._cache[key] = (time.monotonic() + ttl, value)


_service: Optional[AttractionInfoService] = None
_service_lock = threading.Lock()


def get_attraction_info_service() -> AttractionInfoService:
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = AttractionInfoService()
    return _service
