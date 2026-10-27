"""把用户常写的“省份 + 城市”规范化为高德可识别的目的地城市。"""

from __future__ import annotations

import re
import unicodedata


_MUNICIPALITIES = {
    "北京": "北京",
    "北京市": "北京",
    "天津": "天津",
    "天津市": "天津",
    "上海": "上海",
    "上海市": "上海",
    "重庆": "重庆",
    "重庆市": "重庆",
    "香港": "香港",
    "香港特别行政区": "香港",
    "澳门": "澳门",
    "澳门特别行政区": "澳门",
}

_PROVINCE_ALIASES = {
    "河北": ("河北", "河北省"),
    "山西": ("山西", "山西省"),
    "内蒙古": ("内蒙古", "内蒙古自治区"),
    "辽宁": ("辽宁", "辽宁省"),
    "吉林": ("吉林", "吉林省"),
    "黑龙江": ("黑龙江", "黑龙江省"),
    "江苏": ("江苏", "江苏省"),
    "浙江": ("浙江", "浙江省"),
    "安徽": ("安徽", "安徽省"),
    "福建": ("福建", "福建省"),
    "江西": ("江西", "江西省"),
    "山东": ("山东", "山东省"),
    "河南": ("河南", "河南省"),
    "湖北": ("湖北", "湖北省"),
    "湖南": ("湖南", "湖南省"),
    "广东": ("广东", "广东省"),
    "广西": ("广西", "广西壮族自治区"),
    "海南": ("海南", "海南省"),
    "四川": ("四川", "四川省"),
    "贵州": ("贵州", "贵州省"),
    "云南": ("云南", "云南省"),
    "西藏": ("西藏", "西藏自治区"),
    "陕西": ("陕西", "陕西省"),
    "甘肃": ("甘肃", "甘肃省"),
    "青海": ("青海", "青海省"),
    "宁夏": ("宁夏", "宁夏回族自治区"),
    "新疆": ("新疆", "新疆维吾尔自治区"),
    "台湾": ("台湾", "台湾省"),
}

# 长的完整行政区名优先,避免先命中“广西”却留下“壮族自治区”。
_PROVINCE_PREFIXES = sorted(
    {
        alias: canonical
        for canonical, aliases in _PROVINCE_ALIASES.items()
        for alias in aliases
    }.items(),
    key=lambda item: len(item[0]),
    reverse=True,
)

_MUNICIPALITY_PREFIXES = sorted(
    _MUNICIPALITIES.items(),
    key=lambda item: len(item[0]),
    reverse=True,
)

_PREFECTURE_PATTERN = re.compile(r"^(.+?(?:自治州|地区|盟|市))")
_SEPARATORS = re.compile(r"[\s,，、/|·-]+")


def normalize_city_name(value: str) -> str:
    """规范化常见中文字段,同时保留无法安全拆分的原值。

    例如:

    - ``湖南郴州`` / ``湖南省郴州市`` -> ``郴州``
    - ``广西壮族自治区桂林市`` -> ``桂林``
    - ``北京市朝阳区`` -> ``北京``

    省市同名或省名本身可能作为地名时优先保留原值,例如单独的 ``吉林``。
    """
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = _SEPARATORS.sub("", text)
    if text.startswith("中国") and len(text) > 2:
        text = text[2:]
    if not text:
        return ""

    for prefix, canonical in _MUNICIPALITY_PREFIXES:
        if text == prefix or text.startswith(prefix):
            return canonical

    for prefix, canonical in _PROVINCE_PREFIXES:
        if not text.startswith(prefix):
            continue
        remainder = text[len(prefix) :]
        if not remainder:
            return canonical
        if len(remainder) < 2 or remainder in {"市", "区", "县", "州", "盟"}:
            return text
        # “海南藏族自治州”是青海的地名,不是“海南省 + 藏族自治州”。
        if prefix == "海南" and remainder.endswith("藏族自治州"):
            return text
        return _normalize_prefecture(remainder, fallback=text)

    return _normalize_prefecture(text, fallback=text)


def _normalize_prefecture(value: str, *, fallback: str) -> str:
    """从剩下的一段文字中取第一个地级行政区。"""
    match = _PREFECTURE_PATTERN.match(value)
    city = match.group(1) if match else value
    city = city.rstrip("-_")
    if city.endswith("市") and len(city) > 2:
        city = city[:-1]
    return city or fallback
