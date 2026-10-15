"""高德 MCP 桥接层的离线测试

覆盖连接配置、Server 启动参数与 ``root_cause`` 的异常解包。
这几条都对应真实踩过的坑:
- uvx 现场解析依赖导致 MCP 子进程 import 崩溃;
- stdio 模式每次工具调用都新起进程,把单次请求拖到分钟级;
- 崩溃原因被 ExceptionGroup 包住,日志里只剩一句 "unhandled errors"。
"""

import pytest

from app.services import amap_mcp


class _Settings:
    amap_api_key = "test-key"


class _NoKeySettings:
    amap_api_key = ""


class _Tool:
    """最小化的 BaseTool 替身,只需要 name 字段。"""

    def __init__(self, name: str):
        self.name = name


@pytest.fixture
def stub_settings(monkeypatch):
    monkeypatch.setattr(amap_mcp, "get_settings", lambda: _Settings())


@pytest.fixture(autouse=True)
def clean_mcp_env(monkeypatch):
    """清掉可能来自 .env / 环境的 MCP 配置,让断言是确定的。"""
    for name in ("AMAP_MCP_ARGS", "AMAP_MCP_URL", "AMAP_MCP_HOST", "AMAP_MCP_PORT"):
        monkeypatch.delenv(name, raising=False)


class TestConnectionConfig:
    def test_uses_streamable_http_transport(self, stub_settings):
        """回归:传输层必须是常驻 streamable-http。

        stdio 模式**每次工具调用都会新起一个 uvx 进程**,一次 /plan 20+ 次
        工具调用会把请求拖到分钟级;常驻 HTTP 下单次调用只剩网络往返。
        """
        conn = amap_mcp.build_amap_connection()

        assert conn["transport"] == "streamable_http"
        assert conn["url"] == "http://127.0.0.1:8001/mcp"

    def test_url_env_override_means_externally_managed(self, stub_settings, monkeypatch):
        """设置了 AMAP_MCP_URL 就表示 Server 由外部托管,直接采用该地址。"""
        monkeypatch.setenv("AMAP_MCP_URL", "http://mcp.internal:9000/mcp")

        assert amap_mcp.build_amap_connection()["url"] == "http://mcp.internal:9000/mcp"
        assert amap_mcp.amap_server_url() == "http://mcp.internal:9000/mcp"

    def test_host_and_port_env_override(self, stub_settings, monkeypatch):
        monkeypatch.setenv("AMAP_MCP_HOST", "0.0.0.0")
        monkeypatch.setenv("AMAP_MCP_PORT", "8123")

        assert amap_mcp.amap_server_url() == "http://0.0.0.0:8123/mcp"

    def test_requires_api_key(self, monkeypatch):
        monkeypatch.setattr(amap_mcp, "get_settings", lambda: _NoKeySettings())

        with pytest.raises(ValueError, match="AMAP_API_KEY"):
            amap_mcp.build_amap_connection()


class TestServerArgs:
    def test_pins_pydantic_and_selects_http_transport(self):
        """回归:uvx 必须把 pydantic 约束到 2.14 以下,并选 streamable-http。

        amap-mcp-server 硬锁的 ``mcp==1.8.1`` 会导入 ``eval_type_backport``,
        而该符号在 pydantic 2.14 中已被删除。不约束的话,MCP 子进程会在
        import 阶段直接崩溃,``/api/trip/plan`` 全部降级为占位行程。
        """
        args = amap_mcp.server_args()

        assert "pydantic<2.14" in args
        assert args[-1] == "streamable-http"

    def test_honours_env_override(self, monkeypatch):
        monkeypatch.setenv(
            "AMAP_MCP_ARGS", "--with pydantic<2.13 amap-mcp-server streamable-http"
        )

        assert amap_mcp.server_args() == [
            "--with",
            "pydantic<2.13",
            "amap-mcp-server",
            "streamable-http",
        ]


class TestRootCause:
    def test_unwraps_exception_group(self):
        """嵌套的 ExceptionGroup 必须被剥到叶子节点。"""
        inner = ImportError("cannot import name 'eval_type_backport'")
        group = ExceptionGroup("unhandled errors in a TaskGroup", [inner])

        assert amap_mcp.root_cause(group) == (
            "ImportError: cannot import name 'eval_type_backport'"
        )

    def test_unwraps_nested_groups(self):
        leaf = ConnectionResetError("Connection closed")
        inner_group = ExceptionGroup("inner", [leaf])
        outer_group = ExceptionGroup("outer", [inner_group])

        assert amap_mcp.root_cause(outer_group) == "ConnectionResetError: Connection closed"

    def test_handles_plain_exception(self):
        assert amap_mcp.root_cause(RuntimeError("boom")) == "RuntimeError: boom"


class TestToolHelpers:
    def test_find_tool_matches_prefixed_names(self):
        tools = [_Tool("maps_text_search"), _Tool("maps_weather")]

        assert amap_mcp.find_tool(tools, "maps_text_search").name == "maps_text_search"
        # 兼容旧版 HelloAgents 的 amap_ 前缀命名
        assert amap_mcp.find_tool(tools, "amap_maps_weather").name == "maps_weather"
        assert amap_mcp.find_tool(tools, "maps_regeocode") is None

    def test_extract_text_normalizes_shapes(self):
        assert amap_mcp.extract_text("plain") == "plain"
        assert amap_mcp.extract_text([{"text": "a"}, "b"]) == "a\nb"
        assert amap_mcp.extract_text({"text": "c"}) == "c"
        assert amap_mcp.extract_text(123) == "123"
