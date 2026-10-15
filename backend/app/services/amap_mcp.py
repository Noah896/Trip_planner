"""高德地图 MCP 集成(LangChain MCP Adapter)

把高德 MCP Server(``uvx amap-mcp-server``)暴露的工具转换成 LangChain Tool,
供 ``create_agent`` / LangGraph 使用。

传输方式:常驻 streamable-http
----------------------------
原来用的是 ``MultiServerMCPClient`` 的 stdio 模式,而该模式**每次工具调用都会
新建一个 MCP 会话**,也就是每次都重新拉起一个 ``uvx amap-mcp-server`` 子进程
(实测单次数秒)。一次 ``/api/trip/plan`` 会产生 20 次以上的工具调用,叠加起来
直接把单次请求从"秒级"推到"分钟级"。

现在改为:先拉起**一个常驻**的 ``amap-mcp-server streamable-http`` 进程,
再通过 HTTP 连接它。每次工具调用只剩一次本地 HTTP 往返 + 高德接口本身的耗时
(实测约 1.3 秒,且其中绝大部分是 ``restapi.amap.com`` 的网络延迟)。
协议仍然是 MCP,只是传输层从 stdio 换成了 HTTP。

生命周期
--------
- 默认由本模块**惰性**拉起并托管这个常驻进程(首次 ``/plan`` 时启动,而不是在
  应用启动时,避免拖慢启动、也避免启动阶段就依赖网络),应用退出时回收;
- 若设置了 ``AMAP_MCP_URL``,则视为外部托管(例如 docker compose 或单独的进程),
  本模块只负责连接,不插手它的启停。

关于 ``pydantic<2.14``:``amap-mcp-server`` 0.1.11 在元数据里硬锁 ``mcp==1.8.1``,
而 mcp 1.8.1 会从 ``pydantic._internal._typing_extra`` 导入 ``eval_type_backport``
—— 该符号在 pydantic 2.14 中已被删除。由于 ``uvx`` 每次都现场解析依赖(不读本项目
的 venv),不约束的话 MCP 子进程会在 import 阶段直接崩溃。上游若把 ``mcp==1.8.1``
升上去,这个约束即可去掉。
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import time
from typing import Any, Dict, List, Optional

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

from ..config import get_settings

SERVER_NAME = "amap"

# uvx 的启动参数:约束 pydantic(见模块文档) + 指定 streamable-http 传输。
MCP_DEFAULT_ARGS = ["--with", "pydantic<2.14", "amap-mcp-server", "streamable-http"]

# 常驻 MCP Server 的默认监听地址,刻意避开后端自己的 8000。
MCP_DEFAULT_HOST = "127.0.0.1"
MCP_DEFAULT_PORT = 8001
MCP_PATH = "/mcp"

# 首次启动要现解析依赖,可能较慢,给足耐心。
MCP_STARTUP_TIMEOUT = 180.0

_client: Optional[MultiServerMCPClient] = None
_client_url: Optional[str] = None
_tools_cache: Optional[List[BaseTool]] = None
_lock = asyncio.Lock()
_server_lock = asyncio.Lock()
_server_process: Optional[subprocess.Popen] = None


def root_cause(exc: BaseException) -> str:
    """剥掉 ExceptionGroup / TaskGroup 的多层包装,取出最内层的真实原因。

    MCP 传输失败时,anyio 会把底层错误包成嵌套的 ``ExceptionGroup``;直接
    ``str(exc)`` 只会得到 "unhandled errors in a TaskGroup (1 sub-exception)",
    对排障毫无帮助 —— 真正的报错(例如子进程的 ImportError 回溯)全在叶子节点里。

    Args:
        exc: 捕获到的异常

    Returns:
        ``"异常类型: 消息"`` 形式的可读描述
    """
    seen: set = set()
    while id(exc) not in seen:
        seen.add(id(exc))
        children = getattr(exc, "exceptions", None)
        if not children:
            break
        exc = children[0]

    message = str(exc).strip() or "(无错误消息)"
    return f"{type(exc).__name__}: {message}"


# ---------- 地址与启动参数 ----------


def amap_server_host() -> str:
    """常驻 MCP Server 的监听地址。"""
    return os.getenv("AMAP_MCP_HOST", MCP_DEFAULT_HOST)


def amap_server_port() -> int:
    """常驻 MCP Server 的监听端口。"""
    return int(os.getenv("AMAP_MCP_PORT", str(MCP_DEFAULT_PORT)))


def amap_server_url() -> str:
    """常驻 MCP Server 的 HTTP 地址(``AMAP_MCP_URL`` 优先)。"""
    explicit = os.getenv("AMAP_MCP_URL", "").strip()
    if explicit:
        return explicit
    return f"http://{amap_server_host()}:{amap_server_port()}{MCP_PATH}"


def server_args() -> List[str]:
    """MCP Server 的启动参数(``AMAP_MCP_ARGS`` 可整体覆盖)。"""
    raw = os.getenv("AMAP_MCP_ARGS")
    return raw.split() if raw else list(MCP_DEFAULT_ARGS)


def build_amap_connection(url: Optional[str] = None) -> Dict[str, Any]:
    """构建高德 MCP 的 streamable-http 连接配置。"""
    settings = get_settings()

    if not settings.amap_api_key:
        raise ValueError("高德地图 API Key 未配置,请在 .env 文件中设置 AMAP_API_KEY")

    return {
        "transport": "streamable_http",
        "url": url or amap_server_url(),
    }


# ---------- 常驻 Server 的启停 ----------


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """探测端口是否已在监听(本地 TCP 连接,毫秒级)。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


async def _wait_for_port(host: str, port: int, timeout: float) -> float:
    """等待端口就绪,返回实际耗时(秒)。"""
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        if _port_open(host, port):
            return time.monotonic() - started
        await asyncio.sleep(0.3)

    raise TimeoutError(
        f"常驻高德 MCP Server 在 {timeout:.0f} 秒内没有监听 {host}:{port}"
    )


def _spawn_server(host: str, port: int) -> subprocess.Popen:
    """拉起常驻的 amap-mcp-server(streamable-http)。"""
    settings = get_settings()

    env = os.environ.copy()
    env["AMAP_MAPS_API_KEY"] = settings.amap_api_key
    env["FASTMCP_HOST"] = host
    env["FASTMCP_PORT"] = str(port)

    command = os.getenv("AMAP_MCP_COMMAND", "uvx")
    args = server_args()

    print(f"🚀 启动常驻高德 MCP Server: {command} {' '.join(args)}")
    print(f"   监听地址: http://{host}:{port}{MCP_PATH}")

    return subprocess.Popen([command, *args], env=env)


async def ensure_amap_server() -> str:
    """确保常驻高德 MCP Server 可用,返回它的 HTTP 地址。

    - 配置了 ``AMAP_MCP_URL``:直接返回,不插手进程生命周期;
    - 端口已在监听:复用(可能是上一次留下的、或外部启动的实例);
    - 否则惰性拉起一个,并等待它真正开始监听。
    """
    explicit = os.getenv("AMAP_MCP_URL", "").strip()
    if explicit:
        return explicit

    host = amap_server_host()
    port = amap_server_port()
    url = f"http://{host}:{port}{MCP_PATH}"

    global _server_process

    async with _server_lock:
        if _server_process is not None and _server_process.poll() is None:
            return url

        if _port_open(host, port):
            print(f"ℹ️  复用已在监听的高德 MCP Server: {url}")
            _server_process = None
            return url

        _server_process = _spawn_server(host, port)

        try:
            elapsed = await _wait_for_port(host, port, MCP_STARTUP_TIMEOUT)
        except TimeoutError:
            # 起不来就别留着半死不活的进程占端口
            shutdown_amap_server_local()
            raise

        print(f"✅ 常驻高德 MCP Server 已就绪(等待 {elapsed:.1f}s): {url}")
        return url


def shutdown_amap_server_local() -> None:
    """停止本模块拉起的常驻 Server(不清理客户端缓存)。"""
    global _server_process

    proc = _server_process
    _server_process = None

    if proc is None or proc.poll() is not None:
        return

    print("🛑 正在停止常驻高德 MCP Server ...")

    if os.name == "nt":
        # uvx 会再派生一个 python 子进程,必须整棵树一起收,
        # 否则孤儿进程会继续占着端口。
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            check=False,
        )
    else:
        proc.terminate()

    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def shutdown_amap_server() -> None:
    """停止常驻 Server 并清空客户端/工具缓存(应用退出时调用)。"""
    global _client, _client_url, _tools_cache

    shutdown_amap_server_local()
    _client = None
    _client_url = None
    _tools_cache = None


# ---------- 客户端与工具 ----------


async def get_amap_client() -> MultiServerMCPClient:
    """获取高德 MCP 客户端(单例;Server 地址变化时自动重建)"""
    global _client, _client_url

    url = await ensure_amap_server()

    if _client is None or _client_url != url:
        _client = MultiServerMCPClient({SERVER_NAME: build_amap_connection(url)})
        _client_url = url

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
            client = await get_amap_client()

            try:
                _tools_cache = await client.get_tools(server_name=SERVER_NAME)
            except Exception as exc:
                # 失败时不缓存半成品,并把可执行的原因抛给上层 —— 否则调用方
                # 只会看到 "unhandled errors in a TaskGroup",日志等于没说。
                _tools_cache = None
                raise RuntimeError(
                    f"加载高德 MCP 工具失败({root_cause(exc)})。"
                    f"请确认常驻 MCP Server 已启动({amap_server_url()}),"
                    f"并检查后端日志中 amap-mcp-server 子进程的报错回溯。"
                ) from exc

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
    global _client, _client_url, _tools_cache
    _client = None
    _client_url = None
    _tools_cache = None
