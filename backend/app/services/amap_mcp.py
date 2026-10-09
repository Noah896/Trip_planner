"""高德地图 MCP 集成(LangChain MCP Adapter)

将高德 MCP Server(``uvx amap-mcp-server``)暴露的每个工具转换成
LangChain Tool,供 ``create_agent`` / LangGraph 使用。

说明:
    langchain-mcp-adapters 的每次工具调用都会新建一个 MCP 会话,
    因此这里只需对"工具清单"做进程内缓存,无需维护长连接。
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict, List, Optional

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

from ..config import get_settings

SERVER_NAME = "amap"

_client: Optional[MultiServerMCPClient] = None
_tools_cache: Optional[List[BaseTool]] = None
_lock = asyncio.Lock()


def build_amap_connection() -> Dict[str, Any]:
    """构建高德 MCP Server 的 stdio 连接配置。"""
    settings = get_settings()

    if not settings.amap_api_key:
        raise ValueError("高德地图 API Key 未配置,请在 .env 文件中设置 AMAP_API_KEY")

    return {
        "transport": "stdio",
        "command": os.getenv("AMAP_MCP_COMMAND", "uvx"),
        "args": os.getenv("AMAP_MCP_ARGS", "amap-mcp-server").split(),
        "env": {"AMAP_MAPS_API_KEY": settings.amap_api_key},
    }


def get_amap_client() -> MultiServerMCPClient:
    """获取高德 MCP 客户端(单例模式)"""
    global _client

    if _client is None:
        _client = MultiServerMCPClient({SERVER_NAME: build_amap_connection()})

    return _client


async def get_amap_tools(force_refresh: bool = False) -> List[BaseTool]:
    """获取高德 MCP 工具列表(带进程内缓存)

    Args:
        force_refresh: 为 True 时忽略缓存重新拉取

    Returns:
        LangChain Tool 列表
    """
    global _tools_cache

    async with _lock:
        if _tools_cache is None or force_refresh:
            _tools_cache = await get_amap_client().get_tools(server_name=SERVER_NAME)
            names = ", ".join(tool.name for tool in _tools_cache)
            print(f"✅ 已加载高德 MCP 工具 {len(_tools_cache)} 个: {names}")
        return _tools_cache


def find_tool(tools: List[BaseTool], name: str) -> Optional[BaseTool]:
    """按名称查找工具,兼容"带/不带 server 前缀"两种命名方式。

    高德 MCP Server 导出的工具名形如 ``maps_text_search``;
    旧版 HelloAgents 的 auto_expand 会额外加 ``amap_`` 前缀,
    这里两种都能匹配上。
    """
    for tool in tools:
        if tool.name == name:
            return tool
    for tool in tools:
        if tool.name.endswith(name) or name.endswith(tool.name):
            return tool
    return None


def extract_text(result: Any) -> str:
    """把 LangChain / MCP 工具的返回值归一化成文本。"""
    if isinstance(result, str):
        return result
    if isinstance(result, list):
        parts: List[str] = []
        for item in result:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(item.get("text") or json.dumps(item, ensure_ascii=False))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    if isinstance(result, dict):
        return result.get("text") or json.dumps(result, ensure_ascii=False)
    return str(result)


def reset_amap_tools() -> None:
    """重置客户端与工具缓存(用于测试或运行时重新加载)"""
    global _client, _tools_cache
    _client = None
    _tools_cache = None
