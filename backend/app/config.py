"""配置管理模块"""

import os
from pathlib import Path
from typing import List
from pydantic_settings import BaseSettings, SettingsConfigDict
from dotenv import load_dotenv

# 加载环境变量
# 首先尝试加载当前目录的.env
load_dotenv()

# 然后尝试加载同级的 HelloAgents 仓库 .env(如果存在,用于兼容旧配置)
legacy_env = Path(__file__).parent.parent.parent.parent / "HelloAgents" / ".env"
if legacy_env.exists():
    load_dotenv(legacy_env, override=False)  # 不覆盖已有的环境变量


class Settings(BaseSettings):
    """应用配置"""

    # 应用基本配置
    app_name: str = "智能旅行规划助手"
    app_version: str = "1.0.0"
    debug: bool = False
    # 运行环境:development / production。决定是否开启热重载与调试日志。
    app_env: str = "development"

    # 服务器配置
    host: str = "0.0.0.0"
    port: int = 8000

    # CORS配置 - 使用字符串,在代码中分割
    cors_origins: str = "http://localhost:5173,http://localhost:3000,http://127.0.0.1:5173,http://127.0.0.1:3000"

    # 高德地图API配置
    amap_api_key: str = ""

    # Unsplash API配置
    unsplash_access_key: str = ""
    unsplash_secret_key: str = ""

    # LLM配置 (LangChain ChatOpenAI)
    # 统一走 OpenAI 兼容协议,与 .env 中的 LLM_MODEL_ID / LLM_BASE_URL 对应
    llm_model_id: str = ""
    llm_base_url: str = ""
    llm_api_key: str = ""
    modelscope_api_key: str = ""
    llm_temperature: float = 0.7
    llm_timeout: int = 60
    # Qwen3.5 默认开启 thinking(思考)模式,而该模式下服务端会拒绝
    # tool_choice=required/object,导致 response_format 结构化输出直接 400。
    # 关掉它既能让结构化输出生效,也能显著降低 4 次模型调用的耗时。
    # 注意:enable_thinking 是 Qwen(ModelScope/DashScope)专用参数,
    # 若换成其它 OpenAI 兼容服务商并遇到 400,请一并调整 llm_service。
    llm_enable_thinking: bool = False
    # 模型单次输出上限(token)。0 表示不限制。
    # 默认关闭:结构化输出被截断会直接判失败,而实测的失败原因是 schema 不匹配、
    # 不是输出过长,所以这里保持"能用就不加约束"。
    llm_max_tokens: int = 0
    # 单个检索/规划 Agent 的 LangGraph 循环上限。
    # 结构化输出一旦校验失败,模型会把同样的参数重发一遍,不加约束就会一直重试
    # (实测 6 次重试 = 100 秒),这里给出明确的收敛边界。
    agent_recursion_limit: int = 12
    # 整条 /api/trip/plan 的墙钟上限(秒)。
    # LLM_TIMEOUT 只约束"两次读取之间的间隔",对流式响应不构成总时长约束,
    # 所以必须另有一个总闸:超时直接降级,而不是把客户端超时打满。
    plan_total_timeout: int = 240

    # 公共旅行知识 RAG。RAG 是第四路非硬依赖检索;失败只标记 knowledge_degraded,
    # 不会把整份行程判定为 degraded。
    rag_enabled: bool = True
    rag_index_path: str = "data/trip_knowledge_faiss"
    rag_seed_path: str = "knowledge/seed_documents.jsonl"
    rag_auto_bootstrap: bool = True
    rag_embedding_model: str = "BAAI/bge-small-zh-v1.5"
    rag_model_root: str = ""
    rag_device: str = "cpu"
    rag_top_k: int = 3
    rag_vector_k: int = 5
    rag_bm25_k: int = 5
    rag_rrf_k: int = 60
    rag_timeout_seconds: float = 15.0
    rag_max_context_chars: int = 2200
    # 阈值由 38 题中文检索基线校准:
    # 高语义分直接接纳;中等语义分仅在中文 BM25 有足够字词证据时接纳。
    rag_min_vector_relevance: float = 0.62
    rag_lexical_rescue_min_vector_relevance: float = 0.55
    rag_lexical_rescue_min_bm25_score: float = 6.0

    # 服务端会话记忆(SQLite)。相对路径以进程工作目录为基准。
    memory_db_path: str = "data/trip_memory.sqlite3"
    # 会话记忆保留天数;过期记录会在读写时顺带清理。
    memory_session_ttl_days: int = 30

    # 账号鉴权。开启后,行程、聊天和记忆接口都必须携带 Bearer token。
    auth_enabled: bool = True
    auth_db_path: str = "data/trip_auth.sqlite3"
    auth_session_ttl_days: int = 30
    auth_allow_registration: bool = True
    # 非空时,注册必须提供相同邀请码。生产环境应设置该值或关闭公开注册。
    auth_registration_key: str = ""
    # 同一用户名 + 来源 IP 在窗口期内允许的登录失败次数。
    # 达到上限后,在最早一次失败滑出窗口前返回 429。
    auth_login_max_failures: int = 5
    auth_login_failure_window_seconds: int = 900
    # 自托管生命周期管理密钥。为空时管理接口关闭。
    auth_admin_key: str = ""
    auth_password_reset_ttl_minutes: int = 15

    # 按登录用户限制模型调用量。计数持久化到 SQLite,默认按北京时间自然日重置。
    # 表单规划 = 1 单位;结构化聊天 = 1 单位;自然语言聊天按最多 2 次模型调用预占。
    model_quota_enabled: bool = True
    model_quota_db_path: str = "data/trip_model_quota.sqlite3"
    model_quota_daily_call_limit: int = 30
    model_quota_utc_offset_hours: int = 8

    # 兼容旧字段(保留以免破坏其他引用)
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4"

    # 日志配置
    log_level: str = "INFO"

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=False,
        extra="ignore",  # 忽略额外的环境变量
    )

    def get_cors_origins_list(self) -> List[str]:
        """获取CORS origins列表"""
        return [origin.strip() for origin in self.cors_origins.split(',')]

    @property
    def is_development(self) -> bool:
        """是否运行在开发环境(决定 uvicorn 是否开启 reload)。"""
        return self.app_env.strip().lower() in {"dev", "development", "local"}

    def resolve_llm_credentials(self) -> tuple:
        """解析 LLM 凭据

        按优先级从配置/环境变量中取出 (model, api_key, base_url),
        与旧版 HelloAgentsLLM 的多 provider 推断逻辑等价。

        Returns:
            (model, api_key, base_url) 三元组
        """
        api_key = (
            self.llm_api_key
            or self.modelscope_api_key
            or os.getenv("MODELSCOPE_API_KEY")
            or os.getenv("OPENAI_API_KEY")
            or ""
        )
        model = (
            self.llm_model_id
            or os.getenv("LLM_MODEL_ID")
            or self.openai_model
        )
        base_url = (
            self.llm_base_url
            or os.getenv("LLM_BASE_URL")
            or ""
        )
        return model, api_key, base_url


# 创建全局配置实例
settings = Settings()


def get_settings() -> Settings:
    """获取配置实例"""
    return settings


# 验证必要的配置
def validate_config():
    """验证配置是否完整"""
    errors = []
    warnings = []

    if not settings.amap_api_key:
        errors.append("AMAP_API_KEY未配置")

    if (
        settings.auth_enabled
        and settings.auth_allow_registration
        and not (settings.auth_registration_key or "").strip()
        and not settings.is_development
    ):
        errors.append(
            "生产环境开启账号注册时必须配置 AUTH_REGISTRATION_KEY,"
            "或设置 AUTH_ALLOW_REGISTRATION=false"
        )

    if settings.model_quota_enabled and not settings.auth_enabled:
        warnings.append(
            "模型配额依赖登录用户身份;AUTH_ENABLED=false 时不会按用户限流"
        )
    admin_key = (settings.auth_admin_key or "").strip()
    if (
        settings.auth_enabled
        and not admin_key
        and not settings.is_development
    ):
        warnings.append(
            "AUTH_ADMIN_KEY 未配置,账号禁用/启用和密码找回管理接口不会开放"
        )
    if admin_key and not settings.is_development and len(admin_key) < 32:
        errors.append("生产环境 AUTH_ADMIN_KEY 至少需要 32 个字符")

    # LLM 凭据按优先级解析,缺失时只告警不阻断启动
    _, llm_api_key, _ = settings.resolve_llm_credentials()
    if not llm_api_key:
        warnings.append(
            "LLM API Key 未配置(LLM_API_KEY / MODELSCOPE_API_KEY),LLM功能可能无法使用"
        )

    if errors:
        error_msg = "配置错误:\n" + "\n".join(f"  - {e}" for e in errors)
        raise ValueError(error_msg)

    if warnings:
        print("\n⚠️  配置警告:")
        for w in warnings:
            print(f"  - {w}")

    return True


# 打印配置信息(用于调试)
def print_config():
    """打印当前配置(隐藏敏感信息)"""
    print(f"应用名称: {settings.app_name}")
    print(f"版本: {settings.app_version}")
    print(f"服务器: {settings.host}:{settings.port}")
    print(f"高德地图API Key: {'已配置' if settings.amap_api_key else '未配置'}")
    print(
        f"账号鉴权: {'已启用' if settings.auth_enabled else '已关闭'};"
        f" 注册: {'开放' if settings.auth_allow_registration else '关闭'}"
    )
    print(
        "账号管理接口: "
        + ("已启用" if (settings.auth_admin_key or "").strip() else "已关闭")
    )
    print(
        f"模型额度: {'启用' if settings.model_quota_enabled else '关闭'};"
        f" 每日上限: {settings.model_quota_daily_call_limit} 单位"
    )

    # 检查LLM配置
    llm_model, llm_api_key, llm_base_url = settings.resolve_llm_credentials()

    print(f"LLM API Key: {'已配置' if llm_api_key else '未配置'}")
    print(f"LLM Base URL: {llm_base_url}")
    print(f"LLM Model: {llm_model}")
    print(f"日志级别: {settings.log_level}")

