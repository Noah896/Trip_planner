"""POI相关API路由"""

import asyncio
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from typing import List, Optional
from ...services.amap_service import get_amap_service
from ...services.attraction_info_service import get_attraction_info_service
from ...services.wikimedia_photo_service import get_wikimedia_photo_service

router = APIRouter(prefix="/poi", tags=["POI"])
_photo_request_semaphore = asyncio.Semaphore(3)


class POIDetailResponse(BaseModel):
    """POI详情响应"""
    success: bool
    message: str
    data: Optional[dict] = None


def _safe_fallback_url(value: str) -> str:
    """Only allow same-site frontend paths, never an open redirect."""
    if not value.startswith("/") or value.startswith("//"):
        return ""
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc or "\r" in value or "\n" in value:
        return ""
    return value


@router.get(
    "/official-site",
    summary="打开景点官网或介绍页",
    description=(
        "优先解析并跳转到景点官网；找不到可信官网时跳转到该景点的搜索介绍页。"
    ),
)
def open_attraction_official_site(
    name: str = Query(..., min_length=1, max_length=100),
    city: str = Query(default="", max_length=64),
    fallback: str = Query(default="", max_length=2048),
):
    clean_name = " ".join(name.split())
    if not clean_name:
        raise HTTPException(status_code=400, detail="景点名称不能为空")
    info = get_attraction_info_service().resolve(clean_name, " ".join(city.split()))
    destination = info.url
    if info.source != "official":
        destination = _safe_fallback_url(fallback) or info.url
    return RedirectResponse(
        url=destination,
        status_code=307,
        headers={
            "Cache-Control": "public, max-age=600",
            "X-Attraction-Info-Source": info.source,
        },
    )


@router.get(
    "/detail/{poi_id}",
    response_model=POIDetailResponse,
    summary="获取POI详情",
    description="根据POI ID获取详细信息,包括图片"
)
async def get_poi_detail(poi_id: str):
    """
    获取POI详情
    
    Args:
        poi_id: POI ID
        
    Returns:
        POI详情响应
    """
    try:
        amap_service = get_amap_service()
        
        # 调用高德地图POI详情API
        result = await amap_service.get_poi_detail(poi_id)
        
        return POIDetailResponse(
            success=True,
            message="获取POI详情成功",
            data=result
        )
        
    except Exception as e:
        print(f"❌ 获取POI详情失败: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"获取POI详情失败: {str(e)}"
        )


@router.get(
    "/search",
    summary="搜索POI",
    description="根据关键词搜索POI"
)
async def search_poi(keywords: str, city: str = "北京"):
    """
    搜索POI

    Args:
        keywords: 搜索关键词
        city: 城市名称

    Returns:
        搜索结果
    """
    try:
        amap_service = get_amap_service()
        result = await amap_service.search_poi(keywords, city)

        return {
            "success": True,
            "message": "搜索成功",
            "data": result
        }

    except Exception as e:
        print(f"❌ 搜索POI失败: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"搜索POI失败: {str(e)}"
        )


@router.get(
    "/photo",
    summary="获取景点图片",
    description="根据景点名称和城市从 Wikimedia Commons 获取可用图片"
)
async def get_attraction_photo(name: str, city: str = ""):
    """
    获取景点图片

    Args:
        name: 景点名称

    Returns:
        图片URL
    """
    try:
        photo_service = get_wikimedia_photo_service()
        # requests 是阻塞调用;限制并发,避免一次加载多张图时打满上游。
        async with _photo_request_semaphore:
            photo = await asyncio.to_thread(
                photo_service.get_photo,
                name,
                city,
            )

        return {
            "success": True,
            "message": "获取图片成功" if photo else "未找到可用图片",
            "data": {
                "name": name,
                "city": city,
                "photo_url": photo.url if photo else None,
                "provider": photo.provider if photo else "",
                "source_url": photo.source_url if photo else "",
                "title": photo.title if photo else "",
                "scope": photo.scope if photo else "",
                "width": photo.width if photo else 0,
                "height": photo.height if photo else 0,
            }
        }

    except Exception as e:
        print(f"❌ 获取景点图片失败: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"获取景点图片失败: {str(e)}"
        )

